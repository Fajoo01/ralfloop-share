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
static float e4m3_value(int i) {
    int e=(i>>3)&15,m=i&7; return e ? (1.0f+(float)m*0.125f)*std::exp2((float)e-7.0f)
                                      : (float)m*0.001953125f;
}
static float e4m3(float x) {
    float sign=x<0?-1.0f:1.0f,ax=std::min(std::fabs(x),448.0f); int lo=0,hi=126;
    while(lo<hi){int mid=(lo+hi+1)>>1;if(e4m3_value(mid)<=ax)lo=mid;else hi=mid-1;}
    int best=lo;if(best<126){float a=std::fabs(ax-e4m3_value(best)),b=std::fabs(ax-e4m3_value(best+1));if(b<a||(b==a&&((best+1)&1)==0&&(best&1)))best++;}
    return sign*e4m3_value(best);
}
static float e2m1(float x) {
    static const float v[]={0,0.5f,1,1.5f,2,3,4,6};float sign=x<0?-1.0f:1.0f,ax=std::min(std::fabs(x),6.0f);int best=0;
    for(int i=1;i<8;i++){float a=std::fabs(ax-v[best]),b=std::fabs(ax-v[i]);if(b<a||(b==a&&(i&1)==0&&(best&1)))best=i;}return sign*v[best];
}
static float pow2ceil(float x){uint32_t u=bits(x);return from_bits((u&0x7f800000u)+((u&0x7fffffu)?0x800000u:0));}
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
static bool test_quant_formats() {
    for(uint32_t mode=DS4_V41_FP8_E8M0;mode<=DS4_V41_FP4_E4M3;mode++){
        const uint32_t width=mode==DS4_V41_FP4_E4M3?16:32;std::vector<float> in(width),ref;
        for(uint32_t i=0;i<width;i++)in[i]=((int)i-(int)width/2)*0.73125f;ref=in;
        float amax=0;for(float x:ref)amax=std::max(amax,std::fabs(bf16(x)));float scale;
        if(mode==DS4_V41_FP8_E8M0)scale=pow2ceil(std::max(amax,1.0e-4f)/448.0f);
        else if(mode==DS4_V41_FP4_E8M0)scale=pow2ceil(std::max(amax,7.052966104933725e-38f)/6.0f);
        else scale=e4m3(std::max(amax,0.01171875f)/6.0f);
        for(float&x:ref){float v=bf16(x);x=bf16((mode==DS4_V41_FP8_E8M0?e4m3(v/scale):e2m1(v/scale))*scale);}
        T t(in.size()*4u);if(!write(t,in)||!ds4_gpu_dsv41_quantize(t.p,width,1,(ds4_v41_activation_format)mode)||!same(read(t,in.size()),ref,"quant_format"))return false;
    }return true;
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
static bool test_engram() {
    const uint32_t width=32,rows=1;std::vector<float> residual(4*width),kv(5*width),qw(4*width),kw(4*width,1.0f),ref;
    for(size_t i=0;i<residual.size();i++){residual[i]=((int)(i%11)-5)*0.125f;qw[i]=((int)(i%7)-3)*0.25f;}
    for(size_t i=0;i<kv.size();i++)kv[i]=((int)(i%13)-6)*0.0625f;ref=residual;
    for(uint32_t hc=0;hc<4;hc++){float h2=0,k2=0,dot=0;for(uint32_t i=0;i<width;i++){float h=ref[hc*width+i],k=bf16(kv[hc*width+i]);h2+=h*h;k2+=k*k;dot+=h*(qw[hc*width+i]*kw[hc*width+i])*k;}dot*=1/std::sqrt(h2/width+1e-20f)*1/std::sqrt(k2/width+1e-20f)*1/std::sqrt((float)width);float gate=1/(1+std::exp(-std::copysign(std::sqrt(std::max(std::fabs(dot),1e-6f)),dot)));for(uint32_t i=0;i<width;i++)ref[hc*width+i]=bf16(ref[hc*width+i]+gate*bf16(kv[4*width+i]));}
    T r(residual.size()*4u),k(kv.size()*4u),q(qw.size()*4u),w(kw.size()*4u);return write(r,residual)&&write(k,kv)&&write(q,qw)&&write(w,kw)&&ds4_gpu_dsv41_engram_add(r.p,k.p,q.p,w.p,nullptr,width,rows,1e-20f)&&same(read(r,ref.size()),ref,"engram",0.008f);
}
static bool test_index_scores() {
    const uint32_t source=4,rows=2,start=6,ratio=2,heads=32,dim=128;
    std::vector<float> q((size_t)rows*heads*dim),w((size_t)rows*heads),k((size_t)source*dim),ref((size_t)rows*source,-INFINITY);
    for(size_t i=0;i<q.size();i++)q[i]=(i%5==0)?0.5f:-0.25f;
    for(size_t i=0;i<w.size();i++)w[i]=(float)((i%4)+1)/8.0f;
    for(size_t i=0;i<k.size();i++)k[i]=(i%7==0)?0.25f:-0.5f;
    for(uint32_t t=0;t<rows;t++){uint32_t visible=(start+t+1)/ratio;for(uint32_t r=0;r<std::min(source,visible);r++){float sum=0;for(uint32_t h=0;h<heads;h++){float dot=0;for(uint32_t d=0;d<dim;d++)dot+=q[((size_t)t*heads+h)*dim+d]*k[(size_t)r*dim+d];sum+=std::max(dot/64.0f,0.0f)*w[(size_t)t*heads+h];}ref[(size_t)t*source+r]=sum;}}
    T qt(q.size()*4u),wt(w.size()*4u),kt(k.size()*4u),st(ref.size()*4u);
    return write(qt,q)&&write(wt,w)&&write(kt,k)&&
        ds4_gpu_dsv41_indexer_scores_batch(st.p,qt.p,wt.p,kt.p,source,rows,start,ratio)&&
        same(read(st,ref.size()),ref,"index_scores",0.02f);
}
static bool test_index_topk() {
    const uint32_t width=1024,rows=1,start=2047,ratio=2,top=512;
    std::vector<float> scores(width);for(uint32_t i=0;i<width;i++)scores[i]=(float)i;
    T s(scores.size()*4u),out(top*sizeof(int32_t));std::vector<int32_t> got(top);
    if(!write(s,scores)||!ds4_gpu_dsv41_indexer_topk_batch(out.p,s.p,width,rows,start,ratio)||
       !ds4_gpu_tensor_read(out.p,0,got.data(),got.size()*sizeof(int32_t)))return false;
    for(uint32_t i=0;i<top;i++)if(got[i]!=(int32_t)(width-1u-i)){std::fprintf(stderr,"index_topk mismatch[%u]: got=%d expected=%u\n",i,got[i],width-1u-i);return false;}
    return true;
}
int main(){if(!ds4_gpu_init())return 2;bool ok=true;
#define RUN(name) do { bool pass=test_##name(); std::printf("%-18s %s\n",#name,pass?"PASS":"FAIL"); ok=ok&&pass; } while(0)
RUN(bf16);RUN(quant_formats);RUN(rope);RUN(candidates);RUN(carry);RUN(gather);RUN(pool);RUN(engram);RUN(index_scores);RUN(index_topk);
#undef RUN
ds4_gpu_cleanup();std::puts(ok?"v41 CUDA primitive oracle: OK":"v41 CUDA primitive oracle: FAIL");return ok?0:1;}
