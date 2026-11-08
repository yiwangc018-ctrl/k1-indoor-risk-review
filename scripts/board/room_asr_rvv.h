/* Narrow RVV kernels for the unquantized whisper.cpp v1.5.5 CPU backend.
 * F16 products accumulate in F32, matching other SIMD backends' precision.
 */
#ifndef ROOM_ASR_RVV_H
#define ROOM_ASR_RVV_H
#include <stddef.h>
#include <stdint.h>
#include <riscv_vector.h>

static __attribute__((noinline)) float room_dot_f16(int n, const uint16_t *x, const uint16_t *y, const float *half_table) {
    const size_t lanes = __riscv_vsetvlmax_e16m1();
    vfloat32m2_t accum = __riscv_vfmv_v_f_f32m2(0.0f, lanes);
    int i = 0;
    for (; i + (int)lanes <= n; i += (int)lanes) {
        /* GCC 13 lacks vector-half intrinsics. Reuse ggml's exact conversion
         * table with indexed vector loads, requiring only RVV F32 support. */
        vuint32m2_t ai = __riscv_vzext_vf2_u32m2(__riscv_vle16_v_u16m1(x+i, lanes), lanes);
        vuint32m2_t bi = __riscv_vzext_vf2_u32m2(__riscv_vle16_v_u16m1(y+i, lanes), lanes);
        ai = __riscv_vsll_vx_u32m2(ai, 2, lanes);
        bi = __riscv_vsll_vx_u32m2(bi, 2, lanes);
        vfloat32m2_t a = __riscv_vluxei32_v_f32m2(half_table, ai, lanes);
        vfloat32m2_t b = __riscv_vluxei32_v_f32m2(half_table, bi, lanes);
        accum = __riscv_vfmacc_vv_f32m2(accum, a, b, lanes);
    }
    vfloat32m1_t zero = __riscv_vfmv_v_f_f32m1(0.0f, 1);
    float result = __riscv_vfmv_f_s_f32m1_f32(__riscv_vfredusum_vs_f32m2_f32m1(accum, zero, lanes));
    for (; i < n; ++i) result += half_table[x[i]] * half_table[y[i]];
    return result;
}

static __attribute__((noinline)) float room_dot_f32(int n, const float *x, const float *y) {
    const size_t lanes = __riscv_vsetvlmax_e32m1();
    vfloat32m1_t accum = __riscv_vfmv_v_f_f32m1(0.0f, lanes);
    int i = 0;
    for (; i + (int)lanes <= n; i += (int)lanes) {
        vfloat32m1_t a = __riscv_vle32_v_f32m1(x+i, lanes);
        vfloat32m1_t b = __riscv_vle32_v_f32m1(y+i, lanes);
        accum = __riscv_vfmacc_vv_f32m1(accum, a, b, lanes);
    }
    vfloat32m1_t zero = __riscv_vfmv_v_f_f32m1(0.0f, 1);
    float result = __riscv_vfmv_f_s_f32m1_f32(__riscv_vfredusum_vs_f32m1_f32m1(accum, zero, lanes));
    for (; i < n; ++i) result += x[i] * y[i];
    return result;
}
#endif
