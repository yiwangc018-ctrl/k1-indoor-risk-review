#include <math.h>
#include <stdio.h>
#include <string.h>
#include "room_asr_rvv.h"

int main(void) {
    static float half_table[65536];
    for (unsigned i=0; i<65536; ++i) {
        uint16_t bits=(uint16_t)i;
        _Float16 value;
        memcpy(&value,&bits,2);
        half_table[i]=(float)value;
    }
    const int sizes[] = {0,1,7,8,15,16,17,32,63,64,257,512,1024};
    float x[1024], y[1024];
    uint16_t hx[1024], hy[1024];
    for (int i=0; i<1024; ++i) {
        x[i] = ((i*37)%251-125)/128.0f;
        y[i] = ((i*83)%257-128)/128.0f;
        _Float16 a=(_Float16)x[i], b=(_Float16)y[i];
        memcpy(hx+i,&a,2); memcpy(hy+i,&b,2);
    }
    for (size_t j=0; j<sizeof(sizes)/sizeof(sizes[0]); ++j) {
        const int n=sizes[j];
        double expected=0.0;
        for(int i=0;i<n;++i) expected+=(double)x[i]*y[i];
        const float a=room_dot_f32(n,x,y), b=room_dot_f16(n,hx,hy,half_table);
        if(fabs(a-expected)>0.0002+fabs(expected)*0.00002 || fabs(b-expected)>0.0002+fabs(expected)*0.00002) {
            fprintf(stderr,"FAIL n=%d f32=%g f16=%g reference=%g\n",n,a,b,expected);
            return 1;
        }
    }
    puts("PASS: 26 RVV dot-product comparisons, including zero length and partial vectors");
    return 0;
}
