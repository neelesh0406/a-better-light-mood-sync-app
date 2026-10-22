#import <Foundation/Foundation.h>
#import <AppKit/AppKit.h>
#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#import <AudioToolbox/AudioToolbox.h>
#include <dlfcn.h>
#include <math.h>
#include <unistd.h>

// The installed SDK predates ScreenCaptureKit. These declarations describe its
// public ObjC API; classes are loaded from the system framework at runtime.
@interface SCDisplay : NSObject
@property(readonly) CGDirectDisplayID displayID;
@end
@interface SCShareableContent : NSObject
@property(readonly) NSArray<SCDisplay *> *displays;
+ (void)getShareableContentExcludingDesktopWindows:(BOOL)exclude onScreenWindowsOnly:(BOOL)onScreen
    completionHandler:(void (^)(SCShareableContent *, NSError *))completion;
@end
@interface SCContentFilter : NSObject
- (instancetype)initWithDisplay:(SCDisplay *)display excludingWindows:(NSArray *)windows;
@end
@interface SCStreamConfiguration : NSObject
@property size_t width;
@property size_t height;
@property OSType pixelFormat;
@property CMTime minimumFrameInterval;
@property NSInteger queueDepth;
@property BOOL showsCursor;
@property BOOL capturesAudio;
@property BOOL excludesCurrentProcessAudio;
@property NSInteger sampleRate;
@property NSInteger channelCount;
@property CFStringRef colorSpaceName;
@end
@interface SCStream : NSObject
- (instancetype)initWithFilter:(SCContentFilter *)filter configuration:(SCStreamConfiguration *)config delegate:(id)delegate;
- (BOOL)addStreamOutput:(id)output type:(NSInteger)type sampleHandlerQueue:(dispatch_queue_t)queue error:(NSError **)error;
- (void)startCaptureWithCompletionHandler:(void (^)(NSError *))completion;
@end
@protocol SCStreamOutput <NSObject>
- (void)stream:(SCStream *)stream didOutputSampleBuffer:(CMSampleBufferRef)sample ofType:(NSInteger)type;
@end
@protocol SCStreamDelegate <NSObject>
- (void)stream:(SCStream *)stream didStopWithError:(NSError *)error;
@end

static void emit(NSDictionary *object) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:object options:0 error:nil];
    if (!data) return;
    flockfile(stdout);
    fwrite(data.bytes, 1, data.length, stdout); fputc('\n', stdout); fflush(stdout);
    funlockfile(stdout);
}

static NSArray *displayList(void) {
    NSMutableArray *result = [NSMutableArray array];
    for (NSScreen *screen in NSScreen.screens) {
        NSNumber *number = screen.deviceDescription[@"NSScreenNumber"];
        CGDirectDisplayID displayID = number.unsignedIntValue;
        [result addObject:@{@"id": number, @"name": screen.localizedName,
            @"width": @(CGDisplayPixelsWide(displayID)), @"height": @(CGDisplayPixelsHigh(displayID)),
            @"main": @(displayID == CGMainDisplayID())}];
    }
    return result;
}

@interface Capture : NSObject <SCStreamOutput, SCStreamDelegate> {
    double lastVideo, lastAudio, low[16], high[16];
}
@property(strong) SCStream *stream;
@property(strong) dispatch_queue_t queue;
@property(copy) NSString *region;
- (void)start:(CGDirectDisplayID)displayID mode:(NSString *)mode;
@end

@implementation Capture
- (void)start:(CGDirectDisplayID)displayID mode:(NSString *)mode {
    self.queue = dispatch_queue_create("local.luma.samples", dispatch_queue_attr_make_with_qos_class(DISPATCH_QUEUE_SERIAL, QOS_CLASS_USER_INTERACTIVE, 0));
    [(id)NSClassFromString(@"SCShareableContent") getShareableContentExcludingDesktopWindows:NO onScreenWindowsOnly:YES completionHandler:^(SCShareableContent *content, NSError *error) {
        if (error) { emit(@{@"event":@"error", @"message":error.localizedDescription}); exit(1); }
        SCDisplay *chosen = nil;
        for (SCDisplay *display in content.displays) if (display.displayID == displayID) chosen = display;
        if (!chosen) { emit(@{@"event":@"error",@"message":@"Display disconnected. Refresh the display list."}); exit(1); }
        SCContentFilter *filter = [[NSClassFromString(@"SCContentFilter") alloc] initWithDisplay:chosen excludingWindows:@[]];
        SCStreamConfiguration *config = [[NSClassFromString(@"SCStreamConfiguration") alloc] init];
        config.width = 128; config.height = 72; config.pixelFormat = kCVPixelFormatType_32BGRA;
        config.minimumFrameInterval = CMTimeMake(1,30); config.queueDepth = 3; config.showsCursor = NO;
        config.capturesAudio = ![mode isEqual:@"screen"]; config.excludesCurrentProcessAudio = YES;
        config.sampleRate = 48000; config.channelCount = 2; config.colorSpaceName = kCGColorSpaceSRGB;
        self.stream = [[NSClassFromString(@"SCStream") alloc] initWithFilter:filter configuration:config delegate:self];
        NSError *outputError = nil;
        if (![mode isEqual:@"music"]) [self.stream addStreamOutput:self type:0 sampleHandlerQueue:self.queue error:&outputError];
        if (!outputError && ![mode isEqual:@"screen"]) [self.stream addStreamOutput:self type:1 sampleHandlerQueue:self.queue error:&outputError];
        if (outputError) { emit(@{@"event":@"error",@"message":outputError.localizedDescription}); exit(1); }
        [self.stream startCaptureWithCompletionHandler:^(NSError *startError) {
            if (startError) { emit(@{@"event":@"error",@"message":startError.localizedDescription}); exit(1); }
            emit(@{@"event":@"ready",@"display":@(displayID)});
        }];
    }];
}

- (void)stream:(SCStream *)stream didStopWithError:(NSError *)error {
    emit(@{@"event":@"error",@"message":error.localizedDescription}); exit(1);
}

- (void)stream:(SCStream *)stream didOutputSampleBuffer:(CMSampleBufferRef)sample ofType:(NSInteger)type {
    @autoreleasepool {
        double now = NSDate.date.timeIntervalSince1970;
        if (type == 0) {
            CVPixelBufferRef image = CMSampleBufferGetImageBuffer(sample);
            if (!image || now-lastVideo < 1.0/30) return;
            lastVideo = now; [self image:image time:now];
        } else if (type == 1) { [self audio:sample time:now]; }
    }
}

- (void)image:(CVPixelBufferRef)image time:(double)now {
    CVPixelBufferLockBaseAddress(image, kCVPixelBufferLock_ReadOnly);
    const uint8_t *bytes = CVPixelBufferGetBaseAddress(image);
    if (!bytes) { CVPixelBufferUnlockBaseAddress(image,kCVPixelBufferLock_ReadOnly); return; }
    size_t width=CVPixelBufferGetWidth(image), height=CVPixelBufferGetHeight(image), stride=CVPixelBufferGetBytesPerRow(image);
    double bins[24][4] = {{0}}, total=0, valid=0, sr=0, sg=0, sb=0, luma=0;
    for (size_t y=0;y<height;y++) for (size_t x=0;x<width;x++) {
        double nx=(double)x/width, ny=(double)y/height;
        if ([self.region isEqual:@"center"] && (nx<.2||nx>.8||ny<.2||ny>.8)) continue;
        if ([self.region isEqual:@"edges"] && nx>.18&&nx<.82&&ny>.18&&ny<.82) continue;
        if ([self.region isEqual:@"left"] && nx>.5) continue;
        if ([self.region isEqual:@"right"] && nx<.5) continue;
        total++;
        size_t i=y*stride+x*4; double b=bytes[i], g=bytes[i+1], r=bytes[i+2];
        double mx=fmax(r,fmax(g,b)),mn=fmin(r,fmin(g,b)),delta=mx-mn;
        if (mx<14) continue; // Exclude movie letterboxing.
        valid++; sr+=r; sg+=g; sb+=b; luma+=.2126*r+.7152*g+.0722*b;
        if (delta>18) {
            double h=mx==r?(g-b)/delta:mx==g?(b-r)/delta+2:(r-g)/delta+4;
            int bin=MIN(23,(int)(fmod(h*60+360,360)/15));
            double weight=pow(delta/255,2)*(mx/255);
            bins[bin][0]+=r*weight; bins[bin][1]+=g*weight; bins[bin][2]+=b*weight; bins[bin][3]+=weight;
        }
    }
    CVPixelBufferUnlockBaseAddress(image,kCVPixelBufferLock_ReadOnly);
    NSArray *average=valid>0?@[@(lrint(sr/valid)),@(lrint(sg/valid)),@(lrint(sb/valid))]:@[@0,@0,@0];
    int winner=0; for (int i=1;i<24;i++) if (bins[i][3]>bins[winner][3]) winner=i;
    double *best=bins[winner];
    NSArray *vibrant=best[3]>fmax(1,valid*.012)?@[@(lrint(best[0]/best[3])),@(lrint(best[1]/best[3])),@(lrint(best[2]/best[3]))]:average;
    emit(@{@"event":@"video",@"time":@(now),@"average":average,@"vibrant":vibrant,
           @"luma":@(valid>0?luma/valid/255:0),@"coverage":@(total>0?valid/total:0)});
}

- (void)audio:(CMSampleBufferRef)sample time:(double)now {
    CMAudioFormatDescriptionRef desc=CMSampleBufferGetFormatDescription(sample);
    if (!desc) return;
    const AudioStreamBasicDescription *format=CMAudioFormatDescriptionGetStreamBasicDescription(desc);
    if (!format || format->mFormatID!=kAudioFormatLinearPCM || !(format->mFormatFlags&kAudioFormatFlagIsFloat) || format->mBitsPerChannel!=32) return;
    size_t needed=0;
    CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(sample,&needed,NULL,0,NULL,NULL,0,NULL);
    AudioBufferList *list=calloc(1,MAX(needed,sizeof(AudioBufferList)));
    CMBlockBufferRef block=NULL;
    if (CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(sample,NULL,list,needed,NULL,NULL,0,&block)!=noErr) { free(list); return; }
    double sum=0,bass=0,mid=0,treble=0,count=0;
    double a=1-exp(-2*M_PI*200/format->mSampleRate),b=1-exp(-2*M_PI*2500/format->mSampleRate);
    for (UInt32 j=0;j<list->mNumberBuffers;j++) {
        AudioBuffer buffer=list->mBuffers[j];
        if (!buffer.mData) continue;
        const float *values=buffer.mData;
        UInt32 channels=MAX(1,buffer.mNumberChannels);
        for (UInt32 i=0;i<buffer.mDataByteSize/4;i++) {
            int channel=MIN(15,j*channels+i%channels); double x=values[i];
            if (!isfinite(x)) continue;
            low[channel]+=a*(x-low[channel]); high[channel]+=b*(x-high[channel]);
            sum+=x*x; bass+=low[channel]*low[channel]; mid+=pow(high[channel]-low[channel],2); treble+=pow(x-high[channel],2); count++;
        }
    }
    if (block) CFRelease(block); free(list);
    if (count>0 && now-lastAudio>=.025) {
        lastAudio=now;
        emit(@{@"event":@"audio",@"time":@(now),@"rms":@(sqrt(sum/count)),@"bass":@(sqrt(bass/count)),@"mid":@(sqrt(mid/count)),@"treble":@(sqrt(treble/count))});
    }
}
@end

int main(int argc,const char *argv[]) {
    @autoreleasepool {
        if (argc>1 && strcmp(argv[1],"--self-test")==0) {
            // Synthetic buffers only: this path neither enumerates nor captures displays/audio.
            Capture *test=[Capture new]; test.region=@"left";
            CVPixelBufferRef image=NULL;
            CVPixelBufferCreate(kCFAllocatorDefault,128,72,kCVPixelFormatType_32BGRA,NULL,&image);
            CVPixelBufferLockBaseAddress(image,0);
            uint8_t *pixels=CVPixelBufferGetBaseAddress(image);
            size_t stride=CVPixelBufferGetBytesPerRow(image);
            for(int y=0;y<72;y++) for(int x=0;x<128;x++) {
                size_t i=y*stride+x*4;
                pixels[i]=x>64?255:0; pixels[i+1]=0; pixels[i+2]=x<=64?255:0; pixels[i+3]=255;
            }
            CVPixelBufferUnlockBaseAddress(image,0);
            [test image:image time:1]; CVPixelBufferRelease(image);
            size_t count=4800, bytes=count*sizeof(float);
            float *wave=malloc(bytes);
            for(size_t i=0;i<count;i++) wave[i]=.2*sin(2*M_PI*100*i/48000);
            AudioStreamBasicDescription format={48000,kAudioFormatLinearPCM,kAudioFormatFlagIsFloat|kAudioFormatFlagIsPacked,4,1,4,1,32,0};
            CMAudioFormatDescriptionRef description=NULL;
            CMAudioFormatDescriptionCreate(kCFAllocatorDefault,&format,0,NULL,0,NULL,NULL,&description);
            CMBlockBufferRef block=NULL;
            CMBlockBufferCreateWithMemoryBlock(kCFAllocatorDefault,wave,bytes,kCFAllocatorDefault,NULL,0,bytes,0,&block);
            CMSampleTimingInfo timing={CMTimeMake(1,48000),CMTimeMake(0,48000),kCMTimeInvalid};
            size_t sampleSize=4; CMSampleBufferRef sample=NULL;
            CMSampleBufferCreateReady(kCFAllocatorDefault,block,description,count,1,&timing,1,&sampleSize,&sample);
            [test audio:sample time:1];
            CFRelease(sample); CFRelease(block); CFRelease(description);
            return 0;
        }
        [NSApplication sharedApplication];
        if (argc>1 && strcmp(argv[1],"--status")==0) { emit(@{@"permission":@(CGPreflightScreenCaptureAccess()),@"displays":displayList()}); return 0; }
        if (argc>1 && strcmp(argv[1],"--permission")==0) { emit(@{@"permission":@(CGRequestScreenCaptureAccess())}); return 0; }
        if (!CGPreflightScreenCaptureAccess()) {
            emit(@{@"event":@"error",@"message":@"Enable ChatGPT or Luma Capture in System Settings → Privacy & Security → Screen & System Audio Recording, then retry."}); return 1;
        }
        if (!dlopen("/System/Library/Frameworks/ScreenCaptureKit.framework/ScreenCaptureKit",RTLD_LAZY)) {
            emit(@{@"event":@"error",@"message":@"ScreenCaptureKit is unavailable. macOS 13 or later is required."}); return 1;
        }
        Capture *capture=[Capture new];
        capture.region=argc>3?[NSString stringWithUTF8String:argv[3]]:@"whole";
        [capture start:argc>1?(CGDirectDisplayID)strtoul(argv[1],NULL,10):CGMainDisplayID() mode:argc>2?[NSString stringWithUTF8String:argv[2]]:@"screen"];
        dispatch_source_t watchdog=NULL;
        if (argc>4) {
            pid_t parent=(pid_t)strtol(argv[4],NULL,10);
            watchdog=dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER,0,0,dispatch_get_main_queue());
            dispatch_source_set_timer(watchdog,dispatch_time(DISPATCH_TIME_NOW,NSEC_PER_SEC),NSEC_PER_SEC,0);
            dispatch_source_set_event_handler(watchdog,^{ if(getppid()!=parent) exit(0); });
            dispatch_resume(watchdog);
        }
        [[NSRunLoop mainRunLoop] run];
        (void)capture;
        (void)watchdog;
    }
    return 0;
}
