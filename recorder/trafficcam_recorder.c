/*
 * trafficcam_recorder.c - RV1106 VI->RGN OSD->VENC H264 segment recorder
 * Hardware timestamp overlay, no re-encode needed.
 * Usage: trafficcam_recorder -w 1920 -h 1080 -f 15 -s 60 -b 8000 -I 0 -o /var/trafficcam/raw
 *        -b is in Kbps (u32BitRate = b*1000 bps)
 */
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "rk_mpi_mb.h"
#include "rk_mpi_rgn.h"
#include "rk_mpi_sys.h"
#include "rk_mpi_venc.h"
#include "rk_mpi_vi.h"

static int   g_width=1920, g_height=1080, g_fps=15, g_chunk=60, g_bps=8000, g_vi=0;
static char  g_dir[512]="/var/trafficcam/raw";
static volatile bool g_quit=false;

/* 5x7 bitmap font (ASCII 0x20-0x5A), 1 bit/pixel per column */
static const uint8_t F5X7[][5]={
    {0x00,0x00,0x00,0x00,0x00}, /* ' '  20 */
    {0x00,0x00,0x5F,0x00,0x00}, /* '!'  21 */
    {0x00,0x07,0x00,0x07,0x00}, /* '"'  22 */
    {0x14,0x7F,0x14,0x7F,0x14}, /* '#'  23 */
    {0x24,0x2A,0x7F,0x2A,0x12}, /* '$'  24 */
    {0x23,0x13,0x08,0x64,0x62}, /* '%'  25 */
    {0x36,0x49,0x55,0x22,0x50}, /* '&'  26 */
    {0x00,0x05,0x03,0x00,0x00}, /* '\'' 27 */
    {0x00,0x1C,0x22,0x41,0x00}, /* '('  28 */
    {0x00,0x41,0x22,0x1C,0x00}, /* ')'  29 */
    {0x08,0x2A,0x1C,0x2A,0x08}, /* '*'  2A */
    {0x08,0x08,0x3E,0x08,0x08}, /* '+'  2B */
    {0x00,0x50,0x30,0x00,0x00}, /* ','  2C */
    {0x08,0x08,0x08,0x08,0x08}, /* '-'  2D */
    {0x00,0x60,0x60,0x00,0x00}, /* '.'  2E */
    {0x20,0x10,0x08,0x04,0x02}, /* '/'  2F */
    {0x3E,0x51,0x49,0x45,0x3E}, /* '0'  30 */
    {0x00,0x42,0x7F,0x40,0x00}, /* '1'  31 */
    {0x42,0x61,0x51,0x49,0x46}, /* '2'  32 */
    {0x21,0x41,0x45,0x4B,0x31}, /* '3'  33 */
    {0x18,0x14,0x12,0x7F,0x10}, /* '4'  34 */
    {0x27,0x45,0x45,0x45,0x39}, /* '5'  35 */
    {0x3C,0x4A,0x49,0x49,0x30}, /* '6'  36 */
    {0x01,0x71,0x09,0x05,0x03}, /* '7'  37 */
    {0x36,0x49,0x49,0x49,0x36}, /* '8'  38 */
    {0x06,0x49,0x49,0x29,0x1E}, /* '9'  39 */
    {0x00,0x36,0x36,0x00,0x00}, /* ':'  3A */
    {0x00,0x56,0x36,0x00,0x00}, /* ';'  3B */
    {0x08,0x14,0x22,0x41,0x00}, /* '<'  3C */
    {0x14,0x14,0x14,0x14,0x14}, /* '='  3D */
    {0x00,0x41,0x22,0x14,0x08}, /* '>'  3E */
    {0x02,0x01,0x51,0x09,0x06}, /* '?'  3F */
    {0x32,0x49,0x79,0x41,0x3E}, /* '@'  40 */
    {0x7E,0x11,0x11,0x11,0x7E}, /* 'A'  41 */
    {0x7F,0x49,0x49,0x49,0x36}, /* 'B'  42 */
    {0x3E,0x41,0x41,0x41,0x22}, /* 'C'  43 */
    {0x7F,0x41,0x41,0x22,0x1C}, /* 'D'  44 */
    {0x7F,0x49,0x49,0x49,0x41}, /* 'E'  45 */
    {0x7F,0x09,0x09,0x09,0x01}, /* 'F'  46 */
    {0x3E,0x41,0x49,0x49,0x7A}, /* 'G'  47 */
    {0x7F,0x08,0x08,0x08,0x7F}, /* 'H'  48 */
    {0x00,0x41,0x7F,0x41,0x00}, /* 'I'  49 */
    {0x20,0x40,0x41,0x3F,0x01}, /* 'J'  4A */
    {0x7F,0x08,0x14,0x22,0x41}, /* 'K'  4B */
    {0x7F,0x40,0x40,0x40,0x40}, /* 'L'  4C */
    {0x7F,0x02,0x0C,0x02,0x7F}, /* 'M'  4D */
    {0x7F,0x04,0x08,0x10,0x7F}, /* 'N'  4E */
    {0x3E,0x41,0x41,0x41,0x3E}, /* 'O'  4F */
    {0x7F,0x09,0x09,0x09,0x06}, /* 'P'  50 */
    {0x3E,0x41,0x51,0x21,0x5E}, /* 'Q'  51 */
    {0x7F,0x09,0x19,0x29,0x46}, /* 'R'  52 */
    {0x46,0x49,0x49,0x49,0x31}, /* 'S'  53 */
    {0x01,0x01,0x7F,0x01,0x01}, /* 'T'  54 */
    {0x3F,0x40,0x40,0x40,0x3F}, /* 'U'  55 */
    {0x1F,0x20,0x40,0x20,0x1F}, /* 'V'  56 */
    {0x3F,0x40,0x38,0x40,0x3F}, /* 'W'  57 */
    {0x63,0x14,0x08,0x14,0x63}, /* 'X'  58 */
    {0x07,0x08,0x70,0x08,0x07}, /* 'Y'  59 */
    {0x61,0x51,0x49,0x45,0x43}, /* 'Z'  5A */
};

/* OSD dimensions: scale=3, pad=2, 23 chars -> "DD-MM-YYYY HH.MM.SS IST" */
#define SC 3
#define PD 2
#define CW (5*SC+PD)   /* char cell width incl gap */
#define CH (7*SC+2*PD) /* char cell height incl pad */
#define TLEN 23
#define OW (CW*TLEN+PD)
#define OH CH
#define FG 0xFFFFFFFFu  /* white */
#define BG 0xB0000000u  /* semi-transparent black */

static uint32_t *g_obuf=NULL;
static RGN_HANDLE g_rgn=0;

static void render(uint32_t *buf, const char *s)
{
    int i;
    for(i=0;i<OW*OH;i++) buf[i]=BG;
    for(int ci=0;s[ci]&&ci<TLEN;ci++){
        uint8_t c=(uint8_t)s[ci];
        if(c<0x20||c>0x5A) c=0x20;
        const uint8_t *g=F5X7[c-0x20];
        int x0=PD+ci*CW, y0=PD;
        for(int col=0;col<5;col++)
            for(int row=0;row<7;row++){
                uint32_t pix=((g[col]>>row)&1)?FG:BG;
                for(int sy=0;sy<SC;sy++)
                    for(int sx=0;sx<SC;sx++){
                        int px=x0+col*SC+sx, py=y0+row*SC+sy;
                        if(px<OW&&py<OH) buf[py*OW+px]=pix;
                    }
            }
    }
}

static void *osd_fn(void *a)
{
    (void)a;
    while(!g_quit){
        time_t t=time(NULL);
        struct tm *tm=localtime(&t);
        char ts[32];
        snprintf(ts,sizeof(ts),"%02d-%02d-%04d %02d.%02d.%02d IST",
            tm->tm_mday,tm->tm_mon+1,tm->tm_year+1900,
            tm->tm_hour,tm->tm_min,tm->tm_sec);
        render(g_obuf,ts);
        BITMAP_S bm;
        bm.enPixelFormat=(PIXEL_FORMAT_E)RK_FMT_ARGB8888;
        bm.u32Width=OW; bm.u32Height=OH; bm.pData=g_obuf;
        RK_MPI_RGN_SetBitMap(g_rgn,&bm);
        sleep(1);
    }
    return NULL;
}

static FILE  *g_fp=NULL;
static int    g_seg=0;
static time_t g_t0=0;

static void next_seg(void)
{
    if(g_fp) fclose(g_fp);
    g_seg++;
    char path[512];
    snprintf(path,sizeof(path),"%s/seg_%04d.h264",g_dir,g_seg);
    g_fp=fopen(path,"wb");
    g_t0=time(NULL);
    fprintf(stderr,"[recorder] opened segment: %s\n",path);
}

static void *venc_fn(void *a)
{
    (void)a;
    VENC_STREAM_S st;
    st.pstPack=malloc(sizeof(VENC_PACK_S));
    next_seg();
    while(!g_quit){
        if(RK_MPI_VENC_GetStream(0,&st,1000)!=RK_SUCCESS) continue;
        if(g_fp){
            void *d=RK_MPI_MB_Handle2VirAddr(st.pstPack->pMbBlk);
            fwrite(d,1,st.pstPack->u32Len,g_fp);
            fflush(g_fp);
        }
        RK_MPI_VENC_ReleaseStream(0,&st);
        if(time(NULL)-g_t0>=g_chunk) next_seg();
    }
    if(g_fp){fclose(g_fp);g_fp=NULL;}
    free(st.pstPack);
    return NULL;
}

static void sig(int s){(void)s; g_quit=true;}

int main(int argc, char *argv[])
{
    int o;
    while((o=getopt(argc,argv,"w:h:f:s:b:I:o:"))!=-1)
        switch(o){
        case 'w': g_width =atoi(optarg);break;
        case 'h': g_height=atoi(optarg);break;
        case 'f': g_fps   =atoi(optarg);break;
        case 's': g_chunk =atoi(optarg);break;
        case 'b': g_bps   =atoi(optarg);break;
        case 'I': g_vi    =atoi(optarg);break;
        case 'o': strncpy(g_dir,optarg,511);break;
        default:
            fprintf(stderr,"Usage: %s -w W -h H -f fps -s secs -b kbps -I ch -o dir\n",argv[0]);
            return 1;
        }
    mkdir(g_dir,0755);
    signal(SIGINT,sig); signal(SIGTERM,sig);
    RK_MPI_SYS_Init();

    /* VI init */
    {
        VI_DEV_ATTR_S da; memset(&da,0,sizeof(da));
        VI_DEV_BIND_PIPE_S bp; memset(&bp,0,sizeof(bp));
        if(RK_MPI_VI_GetDevAttr(0,&da)==RK_ERR_VI_NOT_CONFIG)
            RK_MPI_VI_SetDevAttr(0,&da);
        if(RK_MPI_VI_GetDevIsEnable(0)!=RK_SUCCESS){
            RK_MPI_VI_EnableDev(0);
            bp.u32Num=1; bp.PipeId[0]=0;
            RK_MPI_VI_SetDevBindPipe(0,&bp);
        }
        VI_CHN_ATTR_S ca; memset(&ca,0,sizeof(ca));
        ca.stIspOpt.u32BufCount=3;
        ca.stIspOpt.enMemoryType=VI_V4L2_MEMORY_TYPE_DMABUF;
        ca.stSize.u32Width=g_width; ca.stSize.u32Height=g_height;
        ca.enPixelFormat=RK_FMT_YUV420SP;
        ca.enCompressMode=COMPRESS_MODE_NONE;
        ca.u32Depth=0;
        RK_MPI_VI_SetChnAttr(0,g_vi,&ca);
        RK_MPI_VI_EnableChn(0,g_vi);
    }

    /* VENC init */
    {
        VENC_CHN_ATTR_S va; memset(&va,0,sizeof(va));
        va.stVencAttr.enType=RK_VIDEO_ID_AVC;
        va.stVencAttr.enPixelFormat=RK_FMT_YUV420SP;
        va.stVencAttr.u32Profile=H264E_PROFILE_HIGH;
        va.stVencAttr.u32PicWidth=g_width;
        va.stVencAttr.u32PicHeight=g_height;
        va.stVencAttr.u32VirWidth=g_width;
        va.stVencAttr.u32VirHeight=g_height;
        va.stVencAttr.u32StreamBufCnt=4;
        va.stVencAttr.u32BufSize=g_width*g_height*3/2;
        /* FIXQP: fixed quality, variable bitrate — QP 20 = high quality */
        va.stRcAttr.enRcMode=VENC_RC_MODE_H264FIXQP;
        va.stRcAttr.stH264FixQp.u32Gop=g_fps * 2;
        va.stRcAttr.stH264FixQp.u32IQp=20;  /* I-frame QP  (0=lossless, 51=worst) */
        va.stRcAttr.stH264FixQp.u32PQp=23;  /* P-frame QP */
        va.stRcAttr.stH264FixQp.u32BQp=23;  /* B-frame QP (unused for H264 High, but set anyway) */
        RK_MPI_VENC_CreateChn(0,&va);
        VENC_RECV_PIC_PARAM_S rp; memset(&rp,0,sizeof(rp));
        rp.s32RecvPicNum=-1;
        RK_MPI_VENC_StartRecvFrame(0,&rp);
    }

    /* Bind VI->VENC */
    {
        MPP_CHN_S s={RK_ID_VI,0,g_vi}, d={RK_ID_VENC,0,0};
        RK_MPI_SYS_Bind(&s,&d);
    }

    /* RGN OSD */
    {
        RGN_ATTR_S ra; memset(&ra,0,sizeof(ra));
        ra.enType=OVERLAY_RGN;
        ra.unAttr.stOverlay.enPixelFmt=(PIXEL_FORMAT_E)RK_FMT_ARGB8888;
        ra.unAttr.stOverlay.stSize.u32Width=OW;
        ra.unAttr.stOverlay.stSize.u32Height=OH;
        RK_MPI_RGN_Create(g_rgn,&ra);
        MPP_CHN_S mc={RK_ID_VENC,0,0};
        RGN_CHN_ATTR_S rc; memset(&rc,0,sizeof(rc));
        rc.bShow=RK_TRUE; rc.enType=OVERLAY_RGN;
        rc.unChnAttr.stOverlayChn.stPoint.s32X=20;
        rc.unChnAttr.stOverlayChn.stPoint.s32Y=20;
        rc.unChnAttr.stOverlayChn.u32Layer=0;
        RK_MPI_RGN_AttachToChn(g_rgn,&mc,&rc);
        g_obuf=calloc(OW*OH,4);
    }

    pthread_t ot,vt;
    pthread_create(&ot,NULL,osd_fn,NULL);
    pthread_create(&vt,NULL,venc_fn,NULL);
    while(!g_quit) usleep(100000);
    pthread_join(vt,NULL);
    g_quit=true;
    pthread_join(ot,NULL);

    /* Cleanup */
    {
        MPP_CHN_S mc={RK_ID_VENC,0,0};
        RK_MPI_RGN_DetachFromChn(g_rgn,&mc);
        RK_MPI_RGN_Destroy(g_rgn);
        free(g_obuf);
        MPP_CHN_S s={RK_ID_VI,0,g_vi}, d={RK_ID_VENC,0,0};
        RK_MPI_SYS_UnBind(&s,&d);
        RK_MPI_VI_DisableChn(0,g_vi);
        RK_MPI_VI_DisableDev(0);
        RK_MPI_VENC_StopRecvFrame(0);
        RK_MPI_VENC_DestroyChn(0);
        RK_MPI_SYS_Exit();
    }
    return 0;
}
