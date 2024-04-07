import torch
import triton
import triton.language as tl


@triton.autotune(
    configs=[
        triton.Config(
            {
                "BLOCK_SIZE_M": 128,
                "BLOCK_SIZE_N": 128,
                "BLOCK_SIZE_K": 64,
                "GROUP_SIZE_M": 8,
            },
            num_stages=1,
            num_warps=8,
        ),
    ],
    key=["M", "N", "K"],
)
@triton.jit
def _ternary_bmm_kernel(
    # Pointers to matrices
    a_ptr,
    b_ptr,
    c_ptr,
    # Matrix dimensions
    M,
    N,
    K,
    n_bits,
    # The stride variables represent how much to increase the ptr by when moving by 1
    # element in a particular dimension. E.g. `stride_am` is how much to increase `a_ptr`
    # by to get the element one row down (A has M rows).
    stride_am,
    stride_ak,
    stride_bk,
    stride_bn,
    stride_cm,
    stride_cn,
    stride_batch_a,
    stride_batch_b,
    stride_batch_c,
    # Meta-parameters
    BLOCK_SIZE_M: tl.constexpr,
    BLOCK_SIZE_N: tl.constexpr,
    BLOCK_SIZE_K: tl.constexpr,
    GROUP_SIZE_M: tl.constexpr,
    ACTIVATION: tl.constexpr,
):
    """Kernel for computing the matmul C = A x B.
    A has shape (B, M, K), int8
    B has shape (B, K//n_bits, N), int8, packed
    C has shape (B, M, N),
    """
    # -----------------------------------------------------------
    # Map program ids `pid` to the block of C it should compute.
    # This is done in a grouped ordering to promote L2 data reuse.
    # See above `L2 Cache Optimizations` section for details.
    pid = tl.program_id(axis=0)
    pid_batch = tl.program_id(axis=1)

    num_pid_m = tl.cdiv(M, BLOCK_SIZE_M)
    num_pid_n = tl.cdiv(N, BLOCK_SIZE_N)
    num_pid_in_group = GROUP_SIZE_M * num_pid_n
    group_id = pid // num_pid_in_group
    first_pid_m = group_id * GROUP_SIZE_M
    group_size_m = min(num_pid_m - first_pid_m, GROUP_SIZE_M)
    pid_m = first_pid_m + (pid % group_size_m)
    pid_n = (pid % num_pid_in_group) // group_size_m

    # ----------------------------------------------------------
    # Create pointers for the first blocks of A and B.
    # We will advance this pointer as we move in the K direction
    # and accumulate
    # `a_ptrs` is a block of [BLOCK_SIZE_M, BLOCK_SIZE_K] pointers
    # `b_ptrs` is a block of [BLOCK_SIZE_K, BLOCK_SIZE_N] pointers
    # See above `Pointer Arithmetics` section for details
    offs_am = (pid_m * BLOCK_SIZE_M + tl.arange(0, BLOCK_SIZE_M)) % M
    offs_bn = (pid_n * BLOCK_SIZE_N + tl.arange(0, BLOCK_SIZE_N)) % N
    offs_k = tl.arange(0, BLOCK_SIZE_K)
    a_ptrs = (
        a_ptr
        + (offs_am[:, None] * stride_am + offs_k[None, :] * stride_ak)
        + pid_batch * stride_batch_a
    )
    # b_ptrs = b_ptr + (offs_k[:, None] * stride_bk + offs_bn[None, :] * stride_bn)

    # Adapted from GPTQ-Triton (https://github.com/fpgaminer/GPTQ-triton)
    # b_ptrs is set up such that it repeats elements along the K axis n_bits times
    b_ptrs = b_ptr + (
        (offs_k[:, None] // n_bits) * stride_bk + offs_bn[None, :] * stride_bn
    )
    # (BLOCK_SIZE_K, BLOCK_SIZE_N)
    # shifter is used to extract each bit of each element in the int matrix
    shifter = (offs_k % n_bits)[:, None] * 2

    # -----------------------------------------------------------
    # Iterate to compute a block of the C matrix.
    # We accumulate into a `[BLOCK_SIZE_M, BLOCK_SIZE_N]` block
    # of fp32 values for higher accuracy.
    # `accumulator` will be converted back to fp16 after the loop.
    accumulator = tl.zeros((BLOCK_SIZE_M, BLOCK_SIZE_N), dtype=tl.float32)
    for k in range(0, tl.cdiv(K, BLOCK_SIZE_K)):
        # Load the next block of A and B, generate a mask by checking the K dimension.
        # If it is out of bounds, set it to 0.
        a = tl.load(a_ptrs, mask=offs_k[None, :] < K - k * BLOCK_SIZE_K, other=0.0)
        # b = tl.load(b_ptrs, mask=offs_k[:, None] < K - k * BLOCK_SIZE_K, other=0)
        b = tl.load(b_ptrs)

        # Convert B from int to a.dtype, for each bit in B, 0 becomes -1.0, 1 becomes 1.0
        # b: (BLOCK_SIZE_K, BLOCK_SIZE_N)
        # We extract the 2 bits of each element in the int matrix
        b = (b >> shifter) & 0x3
        # We need to map back the value 2 -> -1
        b = tl.where(b == 0x2, -1, b)

        # Simply convert to a.dtype
        b = b.to(a.dtype)
        # We accumulate along the K dimension.
        accumulator += tl.dot(a, b)
        # Advance the ptrs to the next K block.
        a_ptrs += BLOCK_SIZE_K * stride_ak
        # b_ptrs += BLOCK_SIZE_K * stride_bk
        b_ptrs += (BLOCK_SIZE_K // n_bits) * stride_bk
    # You can fuse arbitrary activation functions here
    # while the accumulator is still in FP32!
    # if ACTIVATION == "leaky_relu":
    #     accumulator = leaky_relu(accumulator)
    c = accumulator.to(tl.float16)

    # -----------------------------------------------------------
    # Write back the block of the output matrix C with masks.
    offs_cm = pid_m * BLOCK_SIZE_M + tl.arange(0, BLOCK_SIZE_M)
    offs_cn = pid_n * BLOCK_SIZE_N + tl.arange(0, BLOCK_SIZE_N)
    c_ptrs = (
        c_ptr
        + stride_cm * offs_cm[:, None]
        + stride_cn * offs_cn[None, :]
        + pid_batch * stride_batch_c
    )
    c_mask = (offs_cm[:, None] < M) & (offs_cn[None, :] < N)
    tl.store(c_ptrs, c, mask=c_mask)


def batched_bitmat(a, b, int_per_2_bits=4, activation=""):
    """
    a: int8 tensor (B, M, K)
    b: int8 packed tensor (N, K // n_bits)
    n_bits: int, number of bits that each element in b represents
    returns a matrix of size B,M,N
    """

    assert a.dim() == 3, "Matrix A must be 3D"
    assert b.dim() == 2, "Matrix B must be 2D"
    assert b.shape[-1] * int_per_2_bits == a.shape[-1], "Incompatible dimensions"
    assert b.is_contiguous(), "A must be contiguous"
    assert a.is_contiguous(), "B must be contiguous"
    assert b.device == a.device, "A and B must be on the same device"

    B, M, K = a.shape
    N, _ = b.shape

    b = b.t()
    b = b.expand(a.shape[0], b.shape[-2], b.shape[-1])
    # Allocates output.
    c = torch.empty((B, M, N), device=b.device, dtype=torch.float16).contiguous()
    # 1D launch kernel where each block gets its own program.
    grid = lambda META: (
        triton.cdiv(M, META["BLOCK_SIZE_M"]) * triton.cdiv(N, META["BLOCK_SIZE_N"]),
        B,
    )

    # print(f"Launching kernel with M = {M}, N = {N}, K = {K}, n_bits = {n_bits}, activation = {activation}")

    # wrap this, otherwise triton tries to launch from cuda:0
    with torch.cuda.device(a.device):
        _ternary_bmm_kernel[grid](
            a,
            b,
            c,
            M,
            N,
            K,
            int_per_2_bits,
            a.stride(1),
            a.stride(2),
            b.stride(1),
            b.stride(2),
            c.stride(1),
            c.stride(2),
            a.stride(0),
            b.stride(0),
            c.stride(0),
            ACTIVATION=activation,
        )
    return c
