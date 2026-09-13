#include "ds4_gpu.h"
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <limits>
#include <vector>

static uint32_t bits(float x) { uint32_t u; memcpy(&u, &x, 4); return u; }
static float from_bits(uint32_t u) { float x; memcpy(&x, &u, 4); return x; }
static float bf16(float x) {
    uint32_t u=bits(x); if ((u&0x7f800000u)!=0x7f800000u) u+=0x7fffu+((u>>16u)&1u);
    return from_bits(u&0xffff0000u);
}
static bool same(const std::vector<float>& a,const std::vector<float>& b,const char *name,float eps=0) {
    if(a.size()!=b.size()) return false;
    for(size_t i=0;i<a.size();i++) {
        if ((std::isinf(a[i])&&std::isinf(b[i])&&std::signbit(a[i])==std::signbit(b[i])) ||
            (std::isnan(a[i])&&std::isnan(b[i])) || std::fabs(a[i]-b[i])<=eps) continue;
        std::fprintf(stderr,"%s mismatch[%zu]: got=%a expected=%a\n",name,i,a[i],b[i]); return false;
    } return true;
}
struct T { ds4_gpu_tensor *p; explicit T(uint64_t bytes):p(ds4_gpu_tensor_alloc(bytes)){} ~T(){ds4_gpu_tensor_free(p);} };
static bool write(T& t,const std::vector<float>& v){return t.p&&ds4_gpu_tensor_write(t.p,0,v.data(),v.size()*4u);}
static std::vector<float> read(T& t,size_t n){std::vector<float> v(n); if(!ds4_gpu_tensor_read(t.p,0,v.data(),n*4u)) v.clear(); return v;}
static bool test_bf16() {
    std::vector<float> in={0.0f,-0.0f,1.00390625f,1.01171875f,-3.1415927f,65504.0f,
        std::numeric_limits<float>::infinity(),-std::numeric_limits<float>::infinity()};
    std::vector<float> ref=in; for(float& x:ref)x=bf16(x); T t(in.size()*4u);
    return write(t,in)&&ds4_gpu_dsv41_quantize(t.p,(uint32_t)in.size(),1,DS4_V41_BF16)&&same(read(t,in.size()),ref,"bf16");
}
static bool test_rope() {
    const uint32_t width=64, heads=2, rows=2, start=7; std::vector<float> in(width*heads*rows),ref;
    for(size_t i=0;i<in.size();i++) in[i]=(float)((int)(i%19)-9)/8.0f; ref=in;
    for(uint32_t r=0;r<rows;r++)for(uint32_t h=0;h<heads;h++)for(uint32_t lane=0;lane<32;lane++){
        float f=1.0f/std::pow(10000.0f,(float)lane/32.0f),th=(float)(start+r)*f,c=std::cos(th),s=std::sin(th);
        size_t i=((size_t)r*heads+h)*width+2u*lane; float re=ref[i],im=ref[i+1];
        ref[i]=bf16(re*c-im*s); ref[i+1]=bf16(re*s+im*c);
    }
    T t(in.size()*4u); return write(t,in)&&ds4_gpu_dsv41_rope(t.p,width,heads,rows,start,false,false)&&same(read(t,in.size()),ref,"rope",0.008f);
}
static bool test_candidates() {
    const uint32_t width=17,rows=2,start=15,ratio=2,blocks=(width+7)/8;
    std::vector<float> scores(width*rows); for(size_t i=0;i<scores.size();i++)scores[i]=(float)i;
    std::vector<float> block_ref(blocks*rows,-INFINITY);
    for(uint32_t r=0;r<rows;r++){uint32_t vis=std::min(width,(start+r+1)/ratio);for(uint32_t b=0;b<blocks;b++)for(uint32_t i=b*8;i<std::min(vis,(b+1)*8);i++)block_ref[r*blocks+b]=std::max(block_ref[r*blocks+b],scores[r*width+i]); if(vis)block_ref[r*blocks+(vis-1)/8]=INFINITY;}
    T s(scores.size()*4u),b(block_ref.size()*4u); if(!write(s,scores)||!ds4_gpu_dsv41_candidate_blocks(b.p,s.p,width,rows,start,ratio)||!same(read(b,block_ref.size()),block_ref,"candidate_blocks"))return false;
    std::vector<float> mask(blocks*rows,0); mask[0]=-INFINITY; mask[blocks+1]=-INFINITY; T m(mask.size()*4u); if(!write(m,mask)||!write(s,scores)||!ds4_gpu_dsv41_candidate_filter(s.p,m.p,width,rows,start,ratio))return false;
    auto ref=scores; for(uint32_t r=0;r<rows;r++){uint32_t vis=std::min(width,(start+r+1)/ratio);for(uint32_t i=0;i<width;i++)if(i>=vis||mask[r*blocks+i/8]!=0)ref[r*width+i]=-INFINITY;}
    return same(read(s,ref.size()),ref,"candidate_filter");
}
static bool test_carry() {
    const uint32_t width=35,rows=2,words=(width+31)/32; std::vector<float> plain(width*rows);
    for(size_t i=0;i<plain.size();i++)plain[i]=(i%3)?-INFINITY:0.0f;
    T p((uint64_t)words*rows*4u),x(plain.size()*4u); if(!write(x,plain)||!ds4_gpu_dsv41_carry_copy(p.p,0,x.p,width,rows,DS4_V41_CARRY_MASK,true))return false;
    std::vector<float> zero(plain.size(),123.0f); if(!write(x,zero)||!ds4_gpu_dsv41_carry_copy(p.p,0,x.p,width,rows,DS4_V41_CARRY_MASK,false)||!same(read(x,plain.size()),plain,"carry_mask"))return false;
    std::vector<float> vals(width*rows);for(size_t i=0;i<vals.size();i++)vals[i]=bf16((float)((int)i-20)/7.0f);
    T bp((uint64_t)((width+1)/2)*rows*4u),bx(vals.size()*4u); if(!write(bx,vals)||!ds4_gpu_dsv41_carry_copy(bp.p,0,bx.p,width,rows,DS4_V41_CARRY_BF16,true))return false;
    if(!write(bx,zero)||!ds4_gpu_dsv41_carry_copy(bp.p,0,bx.p,width,rows,DS4_V41_CARRY_BF16,false))return false;
    return same(read(bx,vals.size()),vals,"carry_bf16");
}
static bool test_gather() {
    const uint32_t source_rows=3, selected=2; std::vector<float> src(source_rows*512);for(size_t i=0;i<src.size();i++)src[i]=(float)i;
    int32_t ids[2]={2,0}; T s(src.size()*4u),o(selected*512u*4u),id(sizeof(ids));
    if(!write(s,src)||!ds4_gpu_tensor_write(id.p,0,ids,sizeof(ids))||!ds4_gpu_dsv41_gather_kv(o.p,s.p,id.p,source_rows,selected))return false;
    std::vector<float> ref(selected*512);memcpy(ref.data(),src.data()+1024,512*4);memcpy(ref.data()+512,src.data(),512*4);
    return same(read(o,ref.size()),ref,"gather");
}
static bool test_pool() {
    const uint32_t width=4,rows=3,start=1,pairs=2; std::vector<float> kv={1,2,3,4, 5,6,7,8, 9,10,11,12},sc={0,0,0,0, 0,0,0,0, 0,0,0,0},prev={-1,-2,-3,-4};
    std::vector<float> ref={0,0,0,0, 7,8,9,10}; for(float&x:ref)x=bf16(x); T k(kv.size()*4u),s(sc.size()*4u),pk(prev.size()*4u),ps(prev.size()*4u),o(pairs*width*4u);
    std::vector<float> zeros(width,0); if(!write(k,kv)||!write(s,sc)||!write(pk,prev)||!write(ps,zeros)||!ds4_gpu_dsv41_pool2(o.p,k.p,s.p,pk.p,ps.p,width,rows,start))return false;
    return same(read(o,ref.size()),ref,"pool2")&&same(read(pk,width),std::vector<float>(kv.begin()+4,kv.begin()+8),"pool_previous");
}
int main(){if(!ds4_gpu_init())return 2;bool ok=test_bf16()&&test_rope()&&test_candidates()&&test_carry()&&test_gather()&&test_pool();ds4_gpu_cleanup();std::puts(ok?"v41 CUDA primitive oracle: OK":"v41 CUDA primitive oracle: FAIL");return ok?0:1;}
