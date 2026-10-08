// ===========================================================================
//  prov.h —— 手机扫码配网（SoftAP + 强制门户 + 配置网页）
//
//  为什么要有这个：板子原来只能连 secrets.h 里写死的那一个 WiFi。路由器改名了、
//  换密码了、把板子拿到别的地方去 —— 就只能插数据线重新编译烧录。现在屏幕开机时
//  画一张二维码，手机扫一下就能换网络。
//
//  三步走：
//    1. 板子开一个自己的热点（esp32-launcher-XXXX），屏幕画 WIFI: 格式二维码
//    2. 手机相机扫码 -> 一步连上热点
//    3. 系统发现"这个网没有外网"-> 自动弹出下面的配置页（强制门户）
//       没弹的话，浏览器打开 192.168.4.1 也一样
//
//  为什么二维码里放 WIFI: 而不是放网址：网址二维码要求手机先手动连上热点才能打开，
//  多一步；WIFI: 格式是扫一下就连上，这是各家智能家居设备的通行做法。
//
//  存哪：NVS 命名空间 "wifi" 的 ssid/pass 两个键。有它就用它，没有就退回
//  secrets.h 的编译期账号 —— 所以老用户升级上来行为不变，整片擦除后也能自愈。
// ===========================================================================
#pragma once

#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>

#define PROV_NVS_NS "wifi"

static String   g_provSsid;          // 热点名（MAC 后两字节拼出来的）
static bool     g_provOn = false;    // 热点现在开着吗
static bool     g_provHaveCred = false;  // NVS 里有没有存过账号
static String   g_provCredSsid, g_provCredPass;

static WebServer g_provHttp(PROV_HTTP_PORT);
static DNSServer g_provDns;
static uint32_t  g_provLastClient = 0;
static uint32_t  g_provConnAt = 0;   // 连上 WiFi 的时刻（0=还没连上）
static bool      g_provScanDone = false;

// ---------------------------------------------------------------------------
// NVS 里的账号
// ---------------------------------------------------------------------------
static void provLoadCred() {
  Preferences p;
  if (!p.begin(PROV_NVS_NS, true)) { g_provHaveCred = false; return; }
  g_provCredSsid = p.getString("ssid", "");
  g_provCredPass = p.getString("pass", "");
  p.end();
  g_provHaveCred = (g_provCredSsid.length() > 0);
}

static bool provSaveCred(const String& ssid, const String& pass) {
  Preferences p;
  if (!p.begin(PROV_NVS_NS, false)) return false;
  p.putString("ssid", ssid);
  p.putString("pass", pass);
  p.end();
  return true;
}

static void provClearCred() {
  Preferences p;
  if (p.begin(PROV_NVS_NS, false)) { p.clear(); p.end(); }
  g_provHaveCred = false;
}

// 现在到底该用哪组账号：NVS 优先，其次 secrets.h
static void provEffectiveCred(String& ssid, String& pass) {
  if (g_provHaveCred) { ssid = g_provCredSsid; pass = g_provCredPass; }
  else                { ssid = WIFI_SSID;      pass = WIFI_PASSWORD; }
}

// ---------------------------------------------------------------------------
// 配置网页
// ---------------------------------------------------------------------------
// 只在第一次打开时扫一遍，之后走缓存 —— 扫描要 2~3 秒，而且扫描期间热点会短暂
// 断一下，每次刷新都扫体验太差。
static String g_provOptions;

static void provBuildOptions(bool force) {
  if (g_provScanDone && !force) return;
  g_provOptions = "";

  int cnt = WiFi.scanNetworks();
  if (cnt > 0) {
    int*  order = (int*)malloc(sizeof(int) * cnt);
    bool* used  = (bool*)malloc(sizeof(bool) * cnt);
    if (order && used) {
      for (int i = 0; i < cnt; i++) used[i] = false;
      // 按信号从强到弱排 —— 常用网络一般都在最上面
      for (int k = 0; k < cnt; k++) {
        int best = -1;
        for (int i = 0; i < cnt; i++) {
          if (used[i]) continue;
          if (best < 0 || WiFi.RSSI(i) > WiFi.RSSI(best)) best = i;
        }
        used[best] = true;
        order[k] = best;
      }
      String seen = "\n";
      for (int k = 0; k < cnt; k++) {
        int i = order[k];
        String s = WiFi.SSID(i);
        if (s.length() == 0) continue;
        if (seen.indexOf("\n" + s + "\n") >= 0) continue;   // 同名只留信号最强的
        seen += s + "\n";
        String esc = s;
        esc.replace("&", "&amp;"); esc.replace("<", "&lt;");
        esc.replace(">", "&gt;");  esc.replace("\"", "&quot;");
        g_provOptions += "<option value=\"" + esc + "\">" + esc +
                         "  (" + String((int)WiFi.RSSI(i)) + "dBm)</option>\n";
      }
    }
    if (order) free(order);
    if (used)  free(used);
  }
  WiFi.scanDelete();
  if (g_provOptions.length() == 0)
    g_provOptions = "<option value=\"\">（没扫到，点下面的“重新扫描”）</option>\n";
  g_provScanDone = true;
}

static String provPage(const String& tip) {
  String cur = g_provHaveCred ? g_provCredSsid : String(WIFI_SSID);
  String h;
  h.reserve(2600);
  h += F("<!DOCTYPE html><html lang=zh><head><meta charset=utf-8>"
         "<meta name=viewport content='width=device-width,initial-scale=1'>"
         "<title>ESP32 启动器 配网</title><style>"
         "body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;margin:0;"
         "padding:18px;background:#0e1116;color:#e6edf3}"
         "h1{font-size:19px;margin:0 0 4px}p.sub{color:#7d8590;font-size:13px;margin:0 0 18px}"
         "label{display:block;font-size:13px;color:#7d8590;margin:14px 0 6px}"
         "select,input{width:100%;box-sizing:border-box;padding:11px;font-size:16px;"
         "border-radius:8px;border:1px solid #2a313b;background:#171c23;color:#e6edf3}"
         "button{width:100%;margin-top:22px;padding:14px;font-size:17px;border:0;"
         "border-radius:8px;background:#4c8bf5;color:#fff;font-weight:600}"
         "a{color:#58a6ff;font-size:13px}"
         ".tip{background:#171c23;border-left:3px solid #d29922;padding:10px 12px;"
         "border-radius:6px;font-size:13px;color:#d29922;margin-bottom:16px}"
         ".ok{border-left-color:#3fb950;color:#3fb950}"
         "</style></head><body>"
         "<h1>ESP32 启动器 · 配网</h1>"
         "<p class=sub>选一个 2.4G 的 WiFi 填上密码，保存后板子会自动重启。</p>");
  if (tip.length()) h += "<div class='tip'>" + tip + "</div>";
  h += "<form method=get action=/save>"
       "<label>WiFi 名字（只支持 2.4G，连不上 5G）</label>"
       "<select name=s>" + g_provOptions + "</select>"
       "<label>或者手动输入名字</label>"
       "<input name=sm placeholder='留空就用上面选的' autocapitalize=off autocorrect=off>"
       "<label>WiFi 密码</label>"
       "<input name=p type=password placeholder='密码' autocapitalize=off autocorrect=off>"
       "<button type=submit>保存并重启</button></form>"
       "<p class=sub style='margin-top:20px'>当前板子在用：<b>" + cur + "</b>"
       " &nbsp;·&nbsp; <a href='/?rescan=1'>重新扫描</a></p>"
       "</body></html>";
  return h;
}

static void provRedirect() {
  g_provHttp.sendHeader("Location", String("http://") + WiFi.softAPIP().toString() + "/", true);
  g_provHttp.send(302, "text/plain", "");
}

static void provHandleRoot() {
  bool rescan = g_provHttp.hasArg("rescan");
  provBuildOptions(rescan);
  g_provHttp.send(200, "text/html; charset=utf-8", provPage(""));
}

static void provHandleSave() {
  String s  = g_provHttp.hasArg("sm") ? g_provHttp.arg("sm") : String("");
  s.trim();
  if (s.length() == 0 && g_provHttp.hasArg("s")) s = g_provHttp.arg("s");
  String p  = g_provHttp.hasArg("p") ? g_provHttp.arg("p") : String("");
  if (s.length() == 0) {
    g_provHttp.send(200, "text/html; charset=utf-8",
                    provPage("没选 WiFi 名字。回去选一个，或者手动填。"));
    return;
  }
  if (provSaveCred(s, p)) {
    Serial.printf("[PROV] saved ssid='%s' pass=%u chars, rebooting\n",
                  s.c_str(), (unsigned)p.length());
    String h = F("<!DOCTYPE html><html lang=zh><head><meta charset=utf-8>"
                 "<meta name=viewport content='width=device-width,initial-scale=1'>"
                 "<title>已保存</title><style>body{font-family:-apple-system,"
                 "'Segoe UI',Roboto,sans-serif;background:#0e1116;color:#e6edf3;"
                 "padding:40px 20px;text-align:center}h1{font-size:20px}"
                 "p{color:#7d8590;font-size:14px;line-height:1.7}</style></head><body>"
                 "<h1>✅ 已保存</h1><p>板子正在重启并连接 <b>");
    h += s;
    h += F("</b><br>看板子屏幕：状态栏出现 IP 就成功了。<br>"
           "这个页面可以关掉了。</p></body></html>");
    g_provHttp.send(200, "text/html; charset=utf-8", h);
    delay(1200);            // 等浏览器真的把响应收完再重启
    ESP.restart();
  } else {
    g_provHttp.send(200, "text/html; charset=utf-8", provPage("写 NVS 失败，再试一次。"));
  }
}

// ---------------------------------------------------------------------------
// 开关
// ---------------------------------------------------------------------------
static void provBegin() {
  provLoadCred();

  uint8_t mac[6];
  WiFi.macAddress(mac);
  char suffix[8];
  snprintf(suffix, sizeof(suffix), "%02X%02X", mac[4], mac[5]);
  g_provSsid = String(PROV_AP_PREFIX) + suffix;

  WiFi.mode(WIFI_AP_STA);
  bool ok = WiFi.softAP(g_provSsid.c_str(), PROV_AP_PASS);
  g_provOn = ok;
  g_provLastClient = millis();

  if (ok) {
    IPAddress ip = WiFi.softAPIP();
    g_provDns.setErrorReplyCode(DNSReplyCode::NoError);
    g_provDns.start(53, "*", ip);          // 把所有域名都指到板子 -> 触发强制门户
    g_provHttp.on("/", provHandleRoot);
    g_provHttp.on("/save", provHandleSave);
    g_provHttp.onNotFound(provRedirect);   // 安卓 /generate_204、苹果 /hotspot-detect.html 都走这
    g_provHttp.begin();
    Serial.printf("[PROV] AP '%s' pass '%s'  http://%s/\n",
                  g_provSsid.c_str(), PROV_AP_PASS, ip.toString().c_str());
    Serial.printf("[PROV] 现在用的是 %s 账号: '%s'\n",
                  g_provHaveCred ? "NVS 里存的" : "secrets.h 编译期的",
                  (g_provHaveCred ? g_provCredSsid : String(WIFI_SSID)).c_str());
  } else {
    Serial.println("[PROV] softAP failed");
  }
}

static void provStop() {
  if (!g_provOn) return;
  g_provHttp.stop();
  g_provDns.stop();
  WiFi.softAPdisconnect(true);
  g_provOn = false;
  Serial.println("[PROV] AP off");
}

// 开机时先把网络列表扫出来预热。实测这一次扫描要 9 秒；如果拖到手机第一次
// 打开配网页时才扫，手机上就是 9 秒白屏 —— iOS/Android 很可能据此判定
// "这个热点上不了网"，直接弹"无法连接"而不给你点保存的机会。
// 放在开机自检画面期间扫掉，等用户看清二维码再扫码时缓存早就好了，页面秒开。
static void provWarmScan() {
  if (!g_provOn) return;
  uint32_t t0 = millis();
  provBuildOptions(true);
  // 数的是 <option> 的个数，不是字符串长度 —— 早先这里打印 g_provOptions.length()，
  // 于是日志里写着"网络列表已预热（2576 个…）"，看着像扫到了两千多个热点。
  int n = 0;
  for (int i = g_provOptions.indexOf("<option"); i >= 0;
       i = g_provOptions.indexOf("<option", i + 1)) n++;
  Serial.printf("[PROV] 网络列表已预热（%d 个网络，HTML %u 字节，耗时 %lums）\n",
                n, (unsigned)g_provOptions.length(), (unsigned long)(millis() - t0));
}

static void provPoll() {
  if (!g_provOn) return;

  g_provDns.processNextRequest();
  g_provHttp.handleClient();

  int n = WiFi.softAPgetStationNum();
  if (n > 0) g_provLastClient = millis();

  // 连上 WiFi 之后热点再留一会儿（方便临时扫码改网络），没客户端就关掉省电。
  // 从没连上过就永远留着 —— 那是唯一的救急入口。
  if (WiFi.status() == WL_CONNECTED) {
    if (g_provConnAt == 0) g_provConnAt = millis();
    if (n == 0 && (uint32_t)(millis() - g_provConnAt) > PROV_AP_GRACE_MS &&
        (uint32_t)(millis() - g_provLastClient) > PROV_AP_GRACE_MS) {
      provStop();
    }
  } else {
    g_provConnAt = 0;
  }
}

// 手机扫的就是这一串。格式见 https://github.com/zxing/zxing/wiki/Barcode-Contents
static String provQrText() {
  String t = "WIFI:T:WPA;S:";
  t += g_provSsid;
  t += ";P:";
  t += PROV_AP_PASS;
  t += ";;";
  return t;
}

// 给主循环用的诊断（串口 w 指令）
static void provPrintState() {
  Serial.printf("[PROV] ap=%s ssid='%s' pass='%s' clients=%d url=http://%s/\n",
                g_provOn ? "on" : "off", g_provSsid.c_str(), PROV_AP_PASS,
                g_provOn ? WiFi.softAPgetStationNum() : 0,
                WiFi.softAPIP().toString().c_str());
  Serial.printf("[PROV] qr='%s'\n", provQrText().c_str());
  Serial.printf("[PROV] 生效账号='%s' 来源=%s\n",
                (g_provHaveCred ? g_provCredSsid : String(WIFI_SSID)).c_str(),
                g_provHaveCred ? "NVS" : "secrets.h");
}
