// Short offline commands using whisper.cpp v1.5.5. Keep decoding bounded;
// never approve a transcript that approaches the token limit.
#include "whisper.h"
#include <chrono>
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <cstdlib>
#include <string>
#include <vector>

static unsigned u16(const unsigned char *p) { return p[0] | (unsigned(p[1])<<8); }
static unsigned u32(const unsigned char *p) { return u16(p) | (u16(p+2)<<16); }

static bool read_wav(const char *name, std::vector<float> &samples) {
    FILE *f=std::fopen(name,"rb");
    if(!f) return false;
    unsigned char h[12];
    if(std::fread(h,1,12,f)!=12 || std::memcmp(h,"RIFF",4) || std::memcmp(h+8,"WAVE",4)) { std::fclose(f); return false; }
    bool format_ok=false;
    while(std::fread(h,1,8,f)==8) {
        const unsigned count=u32(h+4);
        if(count>1024*1024) break;
        std::vector<unsigned char> bytes(count);
        if(std::fread(bytes.data(),1,count,f)!=count) break;
        if(!std::memcmp(h,"fmt ",4) && count>=16)
            format_ok=u16(bytes.data())==1 && u16(bytes.data()+2)==1 && u32(bytes.data()+4)==16000 && u16(bytes.data()+14)==16;
        if(!std::memcmp(h,"data",4) && format_ok && count%2==0 && count>0 && count<=16000*2*6) {
            samples.resize(count/2);
            for(size_t i=0;i<samples.size();++i) samples[i]=int16_t(u16(bytes.data()+2*i))/32768.0f;
            std::fclose(f); return true;
        }
        if(count&1) std::fseek(f,1,SEEK_CUR);
    }
    std::fclose(f); return false;
}

int main(int argc,char **argv) {
    std::string model,audio,output;
    int threads=4,audio_ctx=256,max_tokens=16;
    for(int i=1;i<argc;++i) {
        const std::string key=argv[i];
        if(key=="-nt" || key=="-nf" || key=="-otxt") continue;
        if(i+1>=argc) return 2;
        const char *value=argv[++i];
        if(key=="-m") model=value;
        else if(key=="-f") audio=value;
        else if(key=="-of") output=value;
        else if(key=="-t") threads=std::atoi(value);
        else if(key=="-ac") audio_ctx=std::atoi(value);
        else if(key=="-mt") max_tokens=std::atoi(value);
        else if(key=="-l" && std::strcmp(value,"zh")) return 2;
        else if(key!="-l" && key!="-bs" && key!="-bo" && key!="-ml") return 2;
    }
    if(model.empty() || audio.empty() || output.empty() || threads<1 || threads>8 || max_tokens<8 || max_tokens>32 || audio_ctx<256 || audio_ctx>1500) return 2;
    std::vector<float> samples;
    if(!read_wav(audio.c_str(),samples) || samples.size()/16000.0>audio_ctx/50.0) {
        std::fprintf(stderr,"Expected mono PCM16 16kHz WAV fitting the audio context\n"); return 2;
    }
    const auto start=std::chrono::steady_clock::now();
    auto cp=whisper_context_default_params(); cp.use_gpu=false;
    whisper_context *ctx=whisper_init_from_file_with_params(model.c_str(),cp);
    if(!ctx) return 3;
    auto p=whisper_full_default_params(WHISPER_SAMPLING_GREEDY);
    p.n_threads=threads; p.language="zh"; p.translate=false;
    p.no_context=true; p.single_segment=true; p.no_timestamps=true;
    p.print_realtime=false; p.print_progress=false; p.print_timestamps=false;
    p.greedy.best_of=1; p.temperature=0; p.temperature_inc=0;
    p.audio_ctx=audio_ctx; p.max_tokens=max_tokens;
    p.encoder_begin_callback=[](whisper_context*,whisper_state*,void*) { std::fprintf(stderr,"ENCODER_BEGIN\n"); return true; };
    const int code=whisper_full(ctx,p,samples.data(),int(samples.size()));
    std::string text;
    int tokens=0;
    if(code==0) for(int i=0;i<whisper_full_n_segments(ctx);++i) {
        text+=whisper_full_get_segment_text(ctx,i);
        tokens+=whisper_full_n_tokens(ctx,i);
    }
    // A short startup command needs far fewer tokens. Reject conservatively
    // before a capped sentence could be mistaken for an exact command.
    const bool near_limit=tokens>=max_tokens-4;
    const int result=code!=0?3:near_limit?4:0;
    FILE *out=std::fopen((output+".txt").c_str(),"wb");
    if(out) { std::fwrite(text.data(),1,text.size(),out); std::fclose(out); }
    const double seconds=std::chrono::duration<double>(std::chrono::steady_clock::now()-start).count();
    FILE *meta=std::fopen((output+".runtime.json").c_str(),"w");
    // exit_code is this process's exit status; whisper_return_code is whisper_full's own code.
    if(meta) { std::fprintf(meta,"{\"exit_code\":%d,\"whisper_return_code\":%d,\"seconds\":%.3f,\"text_tokens\":%d,\"max_tokens\":%d,\"near_token_limit\":%s}\n",result,code,seconds,tokens,max_tokens,near_limit?"true":"false"); std::fclose(meta); }
    whisper_print_timings(ctx); whisper_free(ctx);
    std::printf("%s\n",text.c_str());
    return result;
}
