#import <AppKit/AppKit.h>
#import <WebKit/WebKit.h>
#import <ImageIO/ImageIO.h>
#include <signal.h>
#include <unistd.h>

static void makeIcon(NSString *path, size_t size) {
    CGColorSpaceRef space=CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
    CGContextRef c=CGBitmapContextCreate(NULL,size,size,8,size*4,space,kCGImageAlphaPremultipliedLast);
    CGContextScaleCTM(c,size/1024.0,size/1024.0);
    CGPathRef shape=CGPathCreateWithRoundedRect(CGRectMake(40,40,944,944),210,210,NULL);
    CGContextAddPath(c,shape); CGContextClip(c);
    CGFloat colors[]={.19,.15,.29,1,.07,.07,.1,1};
    CGGradientRef gradient=CGGradientCreateWithColorComponents(space,colors,NULL,2);
    CGContextDrawLinearGradient(c,gradient,CGPointMake(200,1024),CGPointMake(800,0),0);
    CGContextSetRGBStrokeColor(c,.76,.66,.98,1); CGContextSetLineWidth(c,98);
    CGContextSetLineCap(c,kCGLineCapRound); CGContextSetLineJoin(c,kCGLineJoinRound);
    CGContextMoveToPoint(c,350,725); CGContextAddLineToPoint(c,350,310); CGContextAddLineToPoint(c,566,310); CGContextStrokePath(c);
    CGContextSetRGBFillColor(c,.76,.66,.98,1); CGContextFillEllipseInRect(c,CGRectMake(675,263,96,96));
    CGContextResetClip(c); CGContextAddPath(c,shape); CGContextSetLineWidth(c,4); CGContextSetRGBStrokeColor(c,.61,.51,.8,.4); CGContextStrokePath(c);
    CGImageRef image=CGBitmapContextCreateImage(c);
    CGImageDestinationRef destination=CGImageDestinationCreateWithURL((__bridge CFURLRef)[NSURL fileURLWithPath:path],CFSTR("public.png"),1,NULL);
    CGImageDestinationAddImage(destination,image,NULL); CGImageDestinationFinalize(destination);
    CFRelease(destination); CGImageRelease(image); CGPathRelease(shape); CGGradientRelease(gradient); CGContextRelease(c); CGColorSpaceRelease(space);
}

@interface Desktop : NSObject <NSApplicationDelegate,NSWindowDelegate,WKNavigationDelegate,WKUIDelegate,WKDownloadDelegate>
@property(strong) NSWindow *window;
@property(strong) WKWebView *web;
@property(strong) NSTask *backend;
@property(strong) NSPipe *output;
@property(strong) NSFileHandle *log;
@property(strong) NSURL *baseURL;
@property(copy) NSString *support;
@property(copy) NSString *pendingOutput;
@property BOOL quitting;
@end

@implementation Desktop
- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    [NSApp setAppearance:[NSAppearance appearanceNamed:NSAppearanceNameDarkAqua]];
    [self menus];
    self.window=[[NSWindow alloc] initWithContentRect:NSMakeRect(0,0,1220,800)
        styleMask:NSWindowStyleMaskTitled|NSWindowStyleMaskClosable|NSWindowStyleMaskMiniaturizable|NSWindowStyleMaskResizable
        backing:NSBackingStoreBuffered defer:NO];
    self.window.title=@"Luma"; self.window.minSize=NSMakeSize(850,620);
    self.window.delegate=self; self.window.releasedWhenClosed=NO;
    self.window.frameAutosaveName=@"LumaMainWindow";
    [self.window center];
    WKWebViewConfiguration *configuration=[WKWebViewConfiguration new];
    self.web=[[WKWebView alloc] initWithFrame:self.window.contentView.bounds configuration:configuration];
    self.web.autoresizingMask=NSViewWidthSizable|NSViewHeightSizable;
    self.web.navigationDelegate=self; self.web.UIDelegate=self;
    self.window.contentView=self.web;
    [self.web loadHTMLString:@"<html><body style='background:#101113;color:#bfadf9;font:16px -apple-system;display:grid;place-items:center;height:90vh'>Starting Luma…</body></html>" baseURL:nil];
    [self.window makeKeyAndOrderFront:nil]; [NSApp activateIgnoringOtherApps:YES];
    [self startBackend];
}
- (void)menus {
    NSMenu *bar=[NSMenu new]; NSMenuItem *appItem=[NSMenuItem new]; [bar addItem:appItem];
    NSMenu *app=[NSMenu new]; appItem.submenu=app;
    [app addItemWithTitle:@"About Luma" action:@selector(orderFrontStandardAboutPanel:) keyEquivalent:@""];
    [app addItem:[NSMenuItem separatorItem]];
    NSMenuItem *stop=[[NSMenuItem alloc] initWithTitle:@"Stop Live Sync & Restore Light" action:@selector(stopSync:) keyEquivalent:@"."]; stop.target=self; [app addItem:stop];
    NSMenuItem *privacy=[[NSMenuItem alloc] initWithTitle:@"Screen & Audio Permissions…" action:@selector(privacy:) keyEquivalent:@""]; privacy.target=self; [app addItem:privacy];
    NSMenuItem *logs=[[NSMenuItem alloc] initWithTitle:@"Show Luma Data Folder" action:@selector(showData:) keyEquivalent:@""]; logs.target=self; [app addItem:logs];
    [app addItem:[NSMenuItem separatorItem]];
    [app addItemWithTitle:@"Hide Luma" action:@selector(hide:) keyEquivalent:@"h"];
    [app addItem:[NSMenuItem separatorItem]];
    [app addItemWithTitle:@"Quit Luma" action:@selector(terminate:) keyEquivalent:@"q"];
    NSMenuItem *editItem=[NSMenuItem new]; [bar addItem:editItem]; editItem.submenu=[[NSMenu alloc] initWithTitle:@"Edit"];
    for (NSArray *item in @[@[@"Undo",@"undo:",@"z"],@[@"Cut",@"cut:",@"x"],@[@"Copy",@"copy:",@"c"],@[@"Paste",@"paste:",@"v"],@[@"Select All",@"selectAll:",@"a"]]) {
        [editItem.submenu addItemWithTitle:item[0] action:NSSelectorFromString(item[1]) keyEquivalent:item[2]];
    }
    NSMenuItem *windowItem=[NSMenuItem new]; [bar addItem:windowItem]; windowItem.submenu=[[NSMenu alloc] initWithTitle:@"Window"];
    [windowItem.submenu addItemWithTitle:@"Minimize" action:@selector(performMiniaturize:) keyEquivalent:@"m"];
    [windowItem.submenu addItemWithTitle:@"Close Window" action:@selector(performClose:) keyEquivalent:@"w"];
    NSMenuItem *open=[[NSMenuItem alloc] initWithTitle:@"Show Luma" action:@selector(showWindow:) keyEquivalent:@"0"]; open.target=self; [windowItem.submenu addItem:open];
    NSApp.mainMenu=bar; NSApp.windowsMenu=windowItem.submenu;
}
- (void)startBackend {
    NSFileManager *files=NSFileManager.defaultManager;
    self.support=[NSHomeDirectory() stringByAppendingPathComponent:@"Library/Application Support/Luma"];
    NSError *error=nil;
    if (![files createDirectoryAtPath:self.support withIntermediateDirectories:YES attributes:@{NSFilePosixPermissions:@0700} error:&error]) { [self fail:error.localizedDescription]; return; }
    NSString *state=[self.support stringByAppendingPathComponent:@"settings.json"];
    NSString *initial=[NSBundle.mainBundle.resourcePath stringByAppendingPathComponent:@"InitialState.json"];
    if (![files fileExistsAtPath:state] && [files fileExistsAtPath:initial]) {
        if (![files copyItemAtPath:initial toPath:state error:&error]) { [self fail:error.localizedDescription]; return; }
        [files setAttributes:@{NSFilePosixPermissions:@0600} ofItemAtPath:state error:nil];
    }
    NSString *logPath=[self.support stringByAppendingPathComponent:@"Luma.log"];
    [files createFileAtPath:logPath contents:nil attributes:@{NSFilePosixPermissions:@0600}];
    self.log=[NSFileHandle fileHandleForWritingAtPath:logPath];
    self.output=[NSPipe pipe]; self.pendingOutput=@"";
    NSString *resources=[NSBundle.mainBundle.resourcePath stringByAppendingPathComponent:@"Controller"];
    self.backend=[NSTask new]; self.backend.executableURL=[NSURL fileURLWithPath:@"/usr/bin/python3"];
    self.backend.arguments=@[@"-B",@"-u",[resources stringByAppendingPathComponent:@"app.py"],@"--port",@"0",@"--parent-pid",[NSString stringWithFormat:@"%d",getpid()]];
    self.backend.currentDirectoryURL=[NSURL fileURLWithPath:resources];
    NSMutableDictionary *environment=[NSProcessInfo.processInfo.environment mutableCopy];
    environment[@"LUMA_STATE_FILE"]=state; environment[@"PYTHONDONTWRITEBYTECODE"]=@"1";
    self.backend.environment=environment; self.backend.standardOutput=self.output; self.backend.standardError=self.log;
    __weak Desktop *weak=self;
    self.output.fileHandleForReading.readabilityHandler=^(NSFileHandle *handle) {
        NSData *data=handle.availableData;
        if (!data.length) { handle.readabilityHandler=nil; return; }
        NSString *chunk=[[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
        dispatch_async(dispatch_get_main_queue(),^{ [weak consume:chunk?:@""]; });
    };
    self.backend.terminationHandler=^(NSTask *task) {
        dispatch_async(dispatch_get_main_queue(),^{
            Desktop *strong=weak; if(!strong)return;
            [[NSFileManager defaultManager] removeItemAtPath:[strong.support stringByAppendingPathComponent:@"Running.json"] error:nil];
            if(strong.quitting) [NSApp replyToApplicationShouldTerminate:YES];
            else [strong fail:@"The local controller stopped. Reopen Luma to reconnect. See Luma → Show Luma Data Folder for the log."];
        });
    };
    if (![self.backend launchAndReturnError:&error]) { [self fail:error.localizedDescription]; return; }
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW,20*NSEC_PER_SEC),dispatch_get_main_queue(),^{
        if(!self.baseURL&&!self.quitting) [self fail:@"Luma could not start its local controller. This build uses the Python 3 runtime installed on this Mac. See the log in Luma → Show Luma Data Folder."];
    });
}
- (void)consume:(NSString *)chunk {
    self.pendingOutput=[self.pendingOutput stringByAppendingString:chunk];
    if (self.pendingOutput.length>16000) self.pendingOutput=[self.pendingOutput substringFromIndex:self.pendingOutput.length-8000];
    NSArray *lines=[self.pendingOutput componentsSeparatedByString:@"\n"];
    for(NSUInteger i=0;i+1<lines.count;i++) {
        NSString *line=lines[i], *prefix=@"Luma is ready at ";
        if([line hasPrefix:prefix]&&!self.baseURL) {
            NSURL *url=[NSURL URLWithString:[line substringFromIndex:prefix.length]];
            if(![url.host isEqual:@"127.0.0.1"]||![url.scheme isEqual:@"http"]||!url.port)continue;
            self.baseURL=url;
            NSData *data=[NSJSONSerialization dataWithJSONObject:@{@"url":url.absoluteString,@"appPid":@(getpid()),@"serverPid":@(self.backend.processIdentifier)} options:0 error:nil];
            [data writeToFile:[self.support stringByAppendingPathComponent:@"Running.json"] atomically:YES];
            [self.web loadRequest:[NSURLRequest requestWithURL:url]];
        }
    }
    self.pendingOutput=lines.lastObject?:@"";
}
- (void)fail:(NSString *)message {
    NSAlert *alert=[NSAlert new]; alert.messageText=@"Luma needs a moment"; alert.informativeText=message?:@"Could not start Luma.";
    [alert addButtonWithTitle:@"OK"]; [alert beginSheetModalForWindow:self.window completionHandler:nil];
}
- (void)showWindow:(id)sender { [self.window makeKeyAndOrderFront:nil]; [NSApp activateIgnoringOtherApps:YES]; }
- (BOOL)applicationShouldHandleReopen:(NSApplication *)sender hasVisibleWindows:(BOOL)visible { [self showWindow:nil]; return YES; }
- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender { return NO; }
- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication *)sender {
    if(!self.backend.running)return NSTerminateNow;
    self.quitting=YES; [self.backend terminate];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW,5*NSEC_PER_SEC),dispatch_get_main_queue(),^{
        if(self.backend.running)kill(self.backend.processIdentifier,SIGKILL);
        [NSApp replyToApplicationShouldTerminate:YES];
    });
    return NSTerminateLater;
}
- (void)privacy:(id)sender { [NSWorkspace.sharedWorkspace openURL:[NSURL URLWithString:@"x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"]]; }
- (void)showData:(id)sender { [NSWorkspace.sharedWorkspace openURL:[NSURL fileURLWithPath:self.support]]; }
- (void)stopSync:(id)sender {
    if(!self.baseURL)return;
    NSURL *config=[NSURL URLWithString:@"/api/config" relativeToURL:self.baseURL];
    [[NSURLSession.sharedSession dataTaskWithURL:config completionHandler:^(NSData *data,NSURLResponse *response,NSError *error){
        if(!data)return; NSDictionary *body=[NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
        NSString *token=body[@"token"]; if(![token isKindOfClass:NSString.class])return;
        NSMutableURLRequest *request=[NSMutableURLRequest requestWithURL:[NSURL URLWithString:@"/api/sync/stop" relativeToURL:self.baseURL]];
        request.HTTPMethod=@"POST"; request.HTTPBody=[@"{}" dataUsingEncoding:NSUTF8StringEncoding];
        [request setValue:@"application/json" forHTTPHeaderField:@"Content-Type"]; [request setValue:token forHTTPHeaderField:@"X-Luma-Token"];
        [[NSURLSession.sharedSession dataTaskWithRequest:request completionHandler:^(NSData *result,NSURLResponse *r,NSError *e){}] resume];
    }] resume];
}
- (void)webView:(WKWebView *)view decidePolicyForNavigationAction:(WKNavigationAction *)action decisionHandler:(void (^)(WKNavigationActionPolicy))decision {
    NSURL *url=action.request.URL;
    if(action.shouldPerformDownload){decision(WKNavigationActionPolicyDownload);return;}
    if([url.scheme isEqual:@"about"]||[url.scheme isEqual:@"blob"]||([url.host isEqual:self.baseURL.host]&&[url.port isEqual:self.baseURL.port]&&[url.scheme isEqual:@"http"])) {decision(WKNavigationActionPolicyAllow);return;}
    if([@[@"https",@"http"] containsObject:url.scheme])[NSWorkspace.sharedWorkspace openURL:url];
    decision(WKNavigationActionPolicyCancel);
}
- (void)webView:(WKWebView *)view navigationAction:(WKNavigationAction *)action didBecomeDownload:(WKDownload *)download { download.delegate=self; }
- (void)webView:(WKWebView *)view navigationResponse:(WKNavigationResponse *)response didBecomeDownload:(WKDownload *)download { download.delegate=self; }
- (void)download:(WKDownload *)download decideDestinationUsingResponse:(NSURLResponse *)response suggestedFilename:(NSString *)filename completionHandler:(void (^)(NSURL *))completion {
    NSSavePanel *panel=[NSSavePanel savePanel]; panel.nameFieldStringValue=filename; panel.canCreateDirectories=YES;
    [panel beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse answer){
        // WKDownload requires a non-existing destination. Let the native save
        // panel handle replacement approval, then remove only that chosen file.
        NSURL *destination=answer==NSModalResponseOK?panel.URL:nil;
        if(destination&&[NSFileManager.defaultManager fileExistsAtPath:destination.path]) [NSFileManager.defaultManager removeItemAtURL:destination error:nil];
        completion(destination);
    }];
}
- (void)webView:(WKWebView *)view runJavaScriptAlertPanelWithMessage:(NSString *)message initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(void))completion {
    NSAlert *alert=[NSAlert new]; alert.messageText=@"Luma"; alert.informativeText=message; [alert addButtonWithTitle:@"OK"];
    [alert beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse response){completion();}];
}
- (void)webView:(WKWebView *)view runJavaScriptConfirmPanelWithMessage:(NSString *)message initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(BOOL))completion {
    NSAlert *alert=[NSAlert new]; alert.messageText=@"Luma"; alert.informativeText=message; [alert addButtonWithTitle:@"Continue"]; [alert addButtonWithTitle:@"Cancel"];
    [alert beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse response){completion(response==NSAlertFirstButtonReturn);}];
}
- (void)webView:(WKWebView *)view runJavaScriptTextInputPanelWithPrompt:(NSString *)prompt defaultText:(NSString *)text initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(NSString *))completion {
    NSAlert *alert=[NSAlert new]; alert.messageText=prompt; [alert addButtonWithTitle:@"Save"]; [alert addButtonWithTitle:@"Cancel"];
    NSTextField *field=[[NSTextField alloc] initWithFrame:NSMakeRect(0,0,300,24)]; field.stringValue=text?:@""; alert.accessoryView=field;
    [alert beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse response){completion(response==NSAlertFirstButtonReturn?field.stringValue:nil);}];
    [alert.window makeFirstResponder:field];
}
@end

int main(int argc,const char *argv[]) {
    @autoreleasepool {
        if(argc>2&&strcmp(argv[1],"--make-icons")==0) {
            NSString *directory=[NSString stringWithUTF8String:argv[2]];
            for(NSNumber *number in @[@16,@32,@128,@256,@512]) {
                int n=number.intValue;
                makeIcon([directory stringByAppendingPathComponent:[NSString stringWithFormat:@"icon_%dx%d.png",n,n]],n);
                makeIcon([directory stringByAppendingPathComponent:[NSString stringWithFormat:@"icon_%dx%d@2x.png",n,n]],n*2);
            }
            return 0;
        }
        NSApplication *app=[NSApplication sharedApplication]; [app setActivationPolicy:NSApplicationActivationPolicyRegular];
        Desktop *delegate=[Desktop new]; app.delegate=delegate; [app run]; (void)delegate;
    }
    return 0;
}
