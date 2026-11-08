"""Generate a separate v1.5.5 ggml source with three explicitly vectorized hot paths."""
from pathlib import Path
import re

source = Path("ggml.c").read_text()
source = source.replace('#include "ggml.h"', '#include "ggml.h"\n#include "room_asr_rvv.h"', 1)
patches = {
    "ggml_vec_dot_f16": "assert(nrc == 1); *s = room_dot_f16(n, x, y, ggml_table_f32_f16); return;",
    "ggml_vec_dot_f32": "assert(nrc == 1); *s = room_dot_f32(n, x, y); return;",
    "ggml_vec_dot_f16_unroll": "for (int row=0; row<GGML_VEC_DOT_UNROLL; ++row) s[row]=room_dot_f16(n, (const uint16_t *)((const char *)xv + row*xs), y, ggml_table_f32_f16); return;",
}
for name, code in patches.items():
    pattern = r"([^\n]*\b" + name + r"\([^;{}]*\)\s*\{)"
    source, count = re.subn(pattern, lambda m: m[1] + "\n    " + code + "\n", source)
    if count != 1:
        raise RuntimeError(f"Expected exactly one v1.5.5 definition of {name}, found {count}")
Path("ggml-room-rvv.c").write_text(source)
