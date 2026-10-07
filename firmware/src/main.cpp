// ===========================================================================
//  ESP32 触摸屏程序启动器 (ESP32 App Launcher)
//
//  屏幕显示 6 个程序卡片，手点一下 -> 通过 WiFi 通知电脑上的
//  launcher_server.py 把对应程序打开（已经开着就切到前台）。
//
//  数据流：
//      手指 -> 卡片命中 -> 发 "LAUNCH <id>\n" -> PC 端(主动连过来的TCP)执行
//
//  为什么 ESP32 当服务端：这样是电脑往外连 ESP32，Windows 防火墙不会拦，
//  不需要管理员权限、不用加防火墙规则。
// ===========================================================================
#include <Arduino.h>
#include <stdarg.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <ArduinoOTA.h>   // 无线升级：不插数据线也能刷固件
#include <qrcode.h>       // ricmoo/QRCode：把配网信息编成二维码画到屏上

#include "board.h"
#include "config.h"
#include "apps.h"
#include "icons.h"     // 从六个 exe 里提出来的真图标（RGB565，64x64）
#include "appstore.h"  // 卡片表的运行时覆盖层（NVS 存文字/颜色，SPIFFS 存图标）
#include "prov.h"      // 手机扫码配网：SoftAP + 强制门户 + 配网网页

LGFX tft;

// ---------------------------------------------------------------------------
// 调色板
// ---------------------------------------------------------------------------
static const uint32_t C_BG        = 0x0E1116;
static const uint32_t C_TILE      = 0x171C23;
static const uint32_t C_TILE_EDGE = 0x2A313B;
static const uint32_t C_TEXT      = 0xE6EDF3;
static const uint32_t C_DIM       = 0x7D8590;
static const uint32_t C_OK        = 0x3FB950;
static const uint32_t C_ERR       = 0xF85149;
static const uint32_t C_WARN      = 0xD29922;

static inline uint32_t mix(uint32_t a, uint32_t b, uint8_t t) {  // t: 0..255
  uint32_t ar = (a >> 16) & 0xFF, ag = (a >> 8) & 0xFF, ab = a & 0xFF;
  uint32_t br = (b >> 16) & 0xFF, bg = (b >> 8) & 0xFF, bb = b & 0xFF;
  return (((ar * (255 - t) + br * t) / 255) << 16) |
         (((ag * (255 - t) + bg * t) / 255) << 8) |
         ((ab * (255 - t) + bb * t) / 255);
}

// ---------------------------------------------------------------------------
// 运行状态
// ---------------------------------------------------------------------------
enum TileState : uint8_t { TS_IDLE = 0, TS_PRESSED, TS_PENDING, TS_OK, TS_ERR };

struct TileRuntime {
  TileState state = TS_IDLE;
  uint32_t  until = 0;    // 结果提示的过期时间
  char      note[20] = {0};
};
static TileRuntime g_tile[APP_COUNT];

static WiFiServer  g_server(TCP_PORT);
static WiFiClient  g_client;
static WiFiUDP     g_udp;
static String      g_rx;
static int         g_link = 0;          // 0=没客户端 1=等握手 2=已握手
static uint32_t    g_helloDeadline = 0;
static uint32_t    g_lastRx = 0;
static uint32_t    g_lastPing = 0;
static uint32_t    g_wifiRetryAt = 0;
static int         g_wifiTries = 0;

static int  g_msgUntil = 0;
static char g_msg[48] = {0};
static bool g_touchDebug = false;
static bool g_touchRawDebug = false;
static bool g_touchProbe = false;      // 'p'：连续打印 XPT2046 的原始 SPI 采样

// 手指按住屏幕时，状态栏实时显示"它认为你点在哪儿"。
// 这是给"点了没反应"准备的：触摸活着但标定偏了，和触摸彻底坏了，
// 在屏幕上是完全一样的现象 —— 有了这行字就能一眼分开。
static bool g_touchLive = false;
static int  g_touchLx = 0, g_touchLy = 0;
static int  g_touchLtile = -1;

// 合成"原始 ADC 值"—— 只给标定向导的自测用。
// 合成屏幕坐标不够：标定向导读的是触摸芯片的裸 ADC（getTouchRaw），
// 所以要有办法直接喂进一对 rx/ry，才能在没有手指的情况下把整个向导跑完。
static bool     g_synthRawOn = false;
static int32_t  g_synthRawX = 0, g_synthRawY = 0;
static uint32_t g_synthRawUntil = 0;

static int g_sw = 320, g_sh = 240;      // 屏幕尺寸（旋转之后）

// ---------------------------------------------------------------------------
// 显示方向 / 反色：三个可以"在屏幕上点着改"的参数
//
// 为什么要有这段：这块屏只能写不能读，写代码的人看不见画面，而 offset_rotation
// 到底该取哪个值只有肉眼能判断 —— 而且它不只有 4 个旋转：Panel_LCD.inl:133
//   _internal_rotation = ((r + _cfg.offset_rotation) & 3) | ((r & 4) ^ (_cfg.offset_rotation & 4));
// 里的 bit2（值 4）是"上下翻转"标志位。所以 offset_rotation 0..7 配合逻辑 rotation
// 一共能表达 8 种朝向（4 旋转 × 2 翻转态），"画面上下颠倒"这种既不是 0/90/180/270
// 的症状也在覆盖范围内。
//
// 启动器使用固定的横屏配置。此前的“开机点击切换朝向”会在触摸
// 误判时把横屏切回竖屏，并把错误值写入 NVS；正常使用不应改变面板方向。
// ---------------------------------------------------------------------------
#define NVS_DISP_NS "disp"

static uint8_t g_dispOff = PANEL_OFFSET_ROTATION;   // 面板 offset_rotation 0..7（bit2 = 翻转）
static uint8_t g_dispRot = SCREEN_ROTATION;         // 逻辑 rotation 0..3
static bool    g_dispInv = (TFT_INVERT != 0);       // 颜色反色
static uint8_t g_bright  = IDLE_BRIGHT;             // 当前背光（applyDisplay 会照它恢复）

static void dispSaveNvs() {
  // 保留函数供串口调试代码调用，但不再持久化显示方向。
}

static void dispLoadNvs() {
  Preferences p;
  if (!p.begin(NVS_DISP_NS, false)) return;
  // 清掉早期固件留下的显示参数，始终采用编译期的横屏配置。
  p.remove("off");
  p.remove("rot");
  p.remove("inv");
  p.remove("ver");
  p.end();
}

// 把三个值真正写进面板，并刷新 g_sw/g_sh。调用方负责重画。
static void applyDisplay() {
  tft.setPanelOffsetRotation(g_dispOff);
  tft.reinitPanel(g_dispRot);        // 只重写 MADCTL，不会碰背光（见 board.h 里的说明）
  tft.invertDisplay(g_dispInv);      // 必须在 rotation 之后
  tft.setBrightness(g_bright);       // 保险：任何路径把背光清了都能救回来
  g_sw = tft.width();
  g_sh = tft.height();
  Serial.printf("[DISP] off=%u rot=%u inv=%u -> %dx%d bright=%u\n",
                (unsigned)g_dispOff, (unsigned)g_dispRot,
                (unsigned)g_dispInv, g_sw, g_sh, (unsigned)g_bright);
}

// ---------------------------------------------------------------------------
// 无线升级（OTA）
//
// 为什么要有：改一行 UI 就得插一次数据线，而这块板的 USB 口已经开始接触不良
// （表现为"有电、但电脑看不到 USB 设备"）。装上 OTA 之后：
//
//     python -m platformio run -e ota -t upload
//
// 就能隔着 WiFi 烧，一根线都不用碰。
//
// 前提是分区表必须有第二个 app 槽 —— platformio.ini 里已经改成 min_spiffs.csv
// （app0/app1 各 1920 KB）。原来的 huge_app.csv 只有一个 3 MB 的 app0，
// OTA 拿不到可写的槽位，连编译都过不去。
//
// 密码不能省：不设密码，同一局域网里任何人都能把你的板子刷掉。
// 这里改了 platformio.ini 的 [env:ota] --auth 也要跟着改。
// ---------------------------------------------------------------------------
#define OTA_PASSWORD  "launcher-ota"
#define OTA_PORT      3232

static bool    g_otaActive = false;
static uint8_t g_otaPct    = 0;

static void otaBegin() {
  ArduinoOTA.setHostname(DEVICE_NAME);      // 和 mDNS 同名：esp32-launcher.local
  ArduinoOTA.setPassword(OTA_PASSWORD);
  ArduinoOTA.setPort(OTA_PORT);

  ArduinoOTA.onStart([]() {
    g_otaActive = true;
    g_otaPct    = 0;
    Serial.printf("[OTA] start (%s)\n",
                  ArduinoOTA.getCommand() == U_FLASH ? "firmware" : "filesystem");
    tft.fillScreen(C_BG);
    tft.setTextDatum(middle_center);
    tft.setFont(&lgfx::fonts::Font2);
    tft.setTextColor(C_TEXT, C_BG);
    tft.drawString("OTA updating", g_sw / 2, g_sh / 2 - 24);
    tft.drawRect(20, g_sh / 2 + 6, g_sw - 40, 16, C_DIM);
  });

  ArduinoOTA.onProgress([](unsigned int done, unsigned int total) {
    if (!total) return;
    uint8_t pct = (uint8_t)((uint64_t)done * 100 / total);
    if (pct == g_otaPct) return;            // 每 1% 才刷一次屏，省得拖慢传输
    g_otaPct = pct;
    tft.fillRect(22, g_sh / 2 + 8, (g_sw - 44) * pct / 100, 12, 0x58A6FF);
    char buf[24];
    snprintf(buf, sizeof(buf), "%u%%", (unsigned)pct);
    tft.setTextDatum(middle_center);
    tft.setFont(&lgfx::fonts::Font2);
    tft.setTextColor(C_TEXT, C_BG);
    tft.drawString(buf, g_sw / 2, g_sh / 2 + 44);
    Serial.printf("[OTA] %u%%\n", (unsigned)pct);
  });

  ArduinoOTA.onEnd([]() {
    Serial.println("[OTA] done, rebooting");
    tft.fillScreen(C_BG);
    tft.setTextDatum(middle_center);
    tft.setFont(&lgfx::fonts::Font2);
    tft.setTextColor(C_OK, C_BG);
    tft.drawString("OTA OK - rebooting", g_sw / 2, g_sh / 2);
  });

  ArduinoOTA.onError([](ota_error_t e) {
    g_otaActive = false;
    Serial.printf("[OTA] error %u\n", (unsigned)e);
    tft.fillScreen(C_BG);
    tft.setTextDatum(middle_center);
    tft.setFont(&lgfx::fonts::Font2);
    tft.setTextColor(C_ERR, C_BG);
    tft.drawString("OTA FAILED", g_sw / 2, g_sh / 2);
  });

  ArduinoOTA.begin();
  Serial.printf("[OTA] ready on %s:%u  (auth %s)\n",
                WiFi.localIP().toString().c_str(), (unsigned)OTA_PORT, OTA_PASSWORD);
}

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------
static void postMsg(const char* fmt, ...) {
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(g_msg, sizeof(g_msg), fmt, ap);
  va_end(ap);
  g_msgUntil = millis() + RESULT_SHOW_MS;
  Serial.printf("[UI] %s\n", g_msg);
}

static const char* linkText() {
  if (WiFi.status() != WL_CONNECTED) return "WiFi --";
  switch (g_link) {
    case 2:  return "PC OK";
    case 1:  return "PC ..";
    default: return "PC --";
  }
}

// ---------------------------------------------------------------------------
// 图标绘制：全部用几何图形画出来，不依赖任何图片/字库
//   (cx,cy) = 中心，s = 图标外接尺寸
// ---------------------------------------------------------------------------
static void drawGlyph(uint8_t kind, int cx, int cy, int s, uint32_t color) {
  int h = s / 2;
  switch (kind) {
    case GLYPH_TERMINAL: {
      tft.drawRoundRect(cx - h, cy - h * 3 / 4, s, h * 3 / 2, 3, color);
      int ax = cx - h * 2 / 3, ay = cy - h / 4;
      tft.drawLine(ax, ay, ax + h / 3, cy, color);
      tft.drawLine(ax + h / 3, cy, ax, cy + h / 4, color);
      tft.drawLine(ax, cy + h / 4, ax, cy + h / 4, color);
      tft.drawFastHLine(cx + h / 8, cy + h / 4, h / 2, color);
      break;
    }
    case GLYPH_BOLT: {
      tft.fillTriangle(cx + h / 5, cy - h,
                       cx - h * 2 / 3, cy + h / 10,
                       cx - h / 12, cy + h / 10, color);
      tft.fillTriangle(cx - h / 5, cy + h,
                       cx + h * 2 / 3, cy - h / 10,
                       cx + h / 12, cy - h / 10, color);
      break;
    }
    case GLYPH_TV: {
      tft.drawRoundRect(cx - h, cy - h * 3 / 5, s, h * 6 / 5, 4, color);
      tft.drawLine(cx - h / 2, cy - h, cx - h / 5, cy - h * 3 / 5, color);
      tft.drawLine(cx + h / 2, cy - h, cx + h / 5, cy - h * 3 / 5, color);
      tft.fillTriangle(cx - h / 5, cy - h / 3,
                       cx - h / 5, cy + h / 3,
                       cx + h / 3, cy, color);
      break;
    }
    case GLYPH_CHART: {
      tft.drawFastHLine(cx - h, cy + h * 3 / 4, s, color);
      tft.drawFastVLine(cx - h, cy - h * 3 / 4, h * 3 / 2, color);
      int bw = s / 6;
      tft.fillRect(cx - h * 2 / 3, cy + h / 8, bw, h * 5 / 8, color);
      tft.fillRect(cx - h / 6,     cy - h / 4, bw, h, color);
      tft.fillRect(cx + h / 3,     cy - h * 2 / 3, bw, h * 11 / 8, color);
      break;
    }
    case GLYPH_GAMEPAD: {
      tft.drawRoundRect(cx - h, cy - h * 3 / 5, s, h * 6 / 5, h / 2, color);
      tft.fillRect(cx - h * 2 / 3, cy - 2, h / 3, 4, color);
      tft.fillRect(cx - h / 2 - 2, cy - h / 5, 4, h * 2 / 5, color);
      tft.fillCircle(cx + h / 3, cy - h / 6, 3, color);
      tft.fillCircle(cx + h / 2, cy + h / 8, 3, color);
      break;
    }
    case GLYPH_CHAT:
    default: {
      tft.fillRoundRect(cx - h, cy - h * 3 / 4, s, h * 3 / 2, h / 3, color);
      tft.fillTriangle(cx - h / 3, cy + h * 3 / 4 - 1,
                       cx - h / 12, cy + h * 3 / 4 - 1,
                       cx - h / 4, cy + h * 5 / 4 - 2, color);
      int r = max(2, s / 10);
      tft.fillCircle(cx - h / 2, cy, r, C_BG);
      tft.fillCircle(cx,        cy, r, C_BG);
      tft.fillCircle(cx + h / 2, cy, r, C_BG);
      break;
    }
  }
}

// ---------------------------------------------------------------------------
// 卡片布局
// ---------------------------------------------------------------------------
struct Rect { int x, y, w, h; };

static Rect tileRect(int i) {
  // 网格随屏幕长宽比自适应：竖屏 2 列 × 3 行，横屏 3 列 × 2 行。
  // 这样卡片在任何一种取向下都接近正方形，图标才不会把标签挤出去。
  const int cols = (g_sw >= g_sh) ? 3 : 2;
  const int rows = (APP_COUNT + cols - 1) / cols;
  int gridY = STATUS_BAR_H;
  int gridH = g_sh - STATUS_BAR_H;
  int w = (g_sw - TILE_GAP * (cols + 1)) / cols;
  int h = (gridH - TILE_GAP * (rows + 1)) / rows;
  int c = i % cols, r = i / cols;
  Rect rc;
  rc.x = TILE_GAP + c * (w + TILE_GAP);
  rc.y = gridY + TILE_GAP + r * (h + TILE_GAP);
  rc.w = w;
  rc.h = h;
  return rc;
}

static int hitTest(int x, int y) {
  for (int i = 0; i < APP_COUNT; i++) {
    Rect r = tileRect(i);
    if (x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h) return i;
  }
  return -1;
}

// ---------------------------------------------------------------------------
// 绘制
// ---------------------------------------------------------------------------

// 开机自检画面：我看不见这块屏，只能靠用户拍的照片判断对错。这块画面把三个
// 最容易搞错、又最难在正常界面里看出来的东西变成照片里一眼可判的图形：
//   * 整屏一圈白边  -> 画面有没有铺满整块面板（没铺满就是缺边或残留旧画面）
//   * 纯白块/纯黑块 -> 有没有反色（拍出来黑白颠倒就是 inv 设错了）
//   * TOP/BOTTOM/LEFT/RIGHT 文字 -> 方向对不对（字倒着就是差 180°）
//   * 六个纯色块 -> RGB / BGR 有没有错序（红绿蓝对不上就是 rgb_order 错了）
static bool     g_bootScreen = false;
static uint32_t g_bootAt     = 0;

static void drawStatusBar() {
  if (g_bootScreen) return;              // 自检画面期间不要往上盖东西

  tft.fillRect(0, 0, g_sw, STATUS_BAR_H, 0x161B22);
  tft.setTextDatum(middle_left);
  tft.setTextColor(C_DIM, 0x161B22);
  tft.setFont(&lgfx::fonts::Font0);

  bool wifiOk = (WiFi.status() == WL_CONNECTED);
  tft.setTextColor(wifiOk ? C_OK : C_ERR, 0x161B22);
  tft.drawString("WiFi", 6, STATUS_BAR_H / 2);

  tft.setTextColor(C_DIM, 0x161B22);
  static String midStr;                       // 必须静态：c_str() 不能指向临时对象
  static char   tapBuf[48];
  const char* mid;
  uint32_t midColor = C_DIM;
  if (g_touchLive) {
    // 手指正按着：实时显示命中的卡片，或"没点中"
    if (g_touchLtile >= 0)
      snprintf(tapBuf, sizeof(tapBuf), "tap %d,%d -> %s",
               g_touchLx, g_touchLy, g_app[g_touchLtile].label);
    else
      snprintf(tapBuf, sizeof(tapBuf), "tap %d,%d  no tile", g_touchLx, g_touchLy);
    mid = tapBuf;
    midColor = (g_touchLtile >= 0) ? C_OK : C_WARN;
  } else if ((int32_t)(millis() - (uint32_t)g_msgUntil) < 0 && g_msg[0]) {
    mid = g_msg;
    midColor = C_TEXT;
  } else if (wifiOk) {
    midStr = WiFi.localIP().toString();
    mid = midStr.c_str();
  } else {
    mid = "connecting...";
  }
  tft.setTextColor(midColor, 0x161B22);
  tft.setTextDatum(middle_center);
  tft.drawString(mid, g_sw / 2, STATUS_BAR_H / 2);

  tft.setTextDatum(middle_right);
  uint32_t lc = (g_link == 2) ? C_OK : (g_link == 1 ? C_WARN : C_DIM);
  tft.setTextColor(lc, 0x161B22);
  tft.drawString(linkText(), g_sw - 6, STATUS_BAR_H / 2);
}

static void drawTile(int i, bool full = true) {
  Rect r = tileRect(i);
  const AppDef& app = g_app[i];
  TileRuntime& rt = g_tile[i];

  uint32_t bg = C_TILE;
  if (rt.state == TS_PRESSED) bg = mix(C_TILE, app.rgb, 90);
  else if (rt.state == TS_OK) bg = mix(C_TILE, C_OK, 50);
  else if (rt.state == TS_ERR) bg = mix(C_TILE, C_ERR, 50);

  uint32_t edge = (rt.state == TS_OK) ? C_OK : (rt.state == TS_ERR ? C_ERR : app.rgb);

  if (full) {
    tft.fillRoundRect(r.x, r.y, r.w, r.h, TILE_RADIUS, bg);
    tft.drawRoundRect(r.x, r.y, r.w, r.h, TILE_RADIUS, edge);
    tft.drawRoundRect(r.x + 1, r.y + 1, r.w - 2, r.h - 2, TILE_RADIUS - 1,
                      mix(edge, bg, 170));
  } else {
    tft.fillRoundRect(r.x + 2, r.y + 2, r.w - 4, r.h - 4, TILE_RADIUS - 1, bg);
  }

  // ---- 图标 ----
  // 从真 exe 里提出来的 64x64 RGB565 位图，带一个品红透明键，所以无论卡片
  // 底色处于哪个状态（idle / pressed / ok / err）都能直接盖上去，不会留下
  // 一块深色方块。
  const int labelH = 16;                 // 底部留给标签的一条
  int boxX = r.x + 4, boxW = r.w - 8;
  int boxY = r.y + 4, boxH = r.h - 4 - labelH;
  int isz  = min(ICON_W, min(boxW, boxH));
  if (isz == ICON_W && isz == ICON_H) {
    tft.pushImage(boxX + (boxW - isz) / 2, boxY + (boxH - isz) / 2,
                  ICON_W, ICON_H, appIcon(i), (uint16_t)ICON_TRANSPARENT);
  } else if (isz >= 20) {
    // 卡片太小放不下原尺寸位图：退回矢量图标，而不是现做一套缩放。
    drawGlyph(app.glyph, r.x + r.w / 2, boxY + boxH / 2,
              (int)(isz * 0.9f), app.rgb);
  }

  tft.setFont(&lgfx::fonts::Font2);
  tft.setTextDatum(middle_center);
  tft.setTextColor(C_TEXT, bg);
  tft.drawString(app.label, r.x + r.w / 2, r.y + r.h - 11);

  // 状态角标
  const char* badge = nullptr;
  uint32_t bc = C_DIM;
  if (rt.state == TS_PENDING) { badge = "..."; bc = C_WARN; }
  else if (rt.state == TS_OK) { badge = "OK";  bc = C_OK; }
  else if (rt.state == TS_ERR){ badge = "!";   bc = C_ERR; }
  if (badge) {
    tft.setFont(&lgfx::fonts::Font0);
    tft.setTextDatum(top_right);
    tft.setTextColor(bc, bg);
    tft.drawString(badge, r.x + r.w - 6, r.y + 4);
  }
}

static void drawGrid() {
  tft.fillScreen(C_BG);
  for (int i = 0; i < APP_COUNT; i++) drawTile(i);
  drawStatusBar();
}

// ---------------------------------------------------------------------------
// 配置端口（8269）—— 桌面配置器专用
//
// 为什么单独开一个端口而不是复用 8266：8266 那条连接是 PC 端常驻服务独占的
// （pollClient 只在 g_link==0 时才 accept 新连接），配置器硬挤进去会把服务顶掉，
// 两边会不停地互相重连打架。独立端口 = 零争用，而且不用动 PC 端服务、
// 也不用加防火墙规则（始终是电脑往板子连）。
//
// 协议是纯文本行（\n 结尾），和 8266 那边一样，用 telnet 就能手测：
//   HELLO <token>                       -> OK <device> <count>
//   GET                                 -> APP <i> <id> <iconFs> <rrggbb> <label>  xN
//                                          END
//   SET <i> <id> <rrggbb> <label...>    -> OK
//   ICON <i> <bytes>                    -> READY，紧跟 <bytes> 个裸字节（RGB565 小端）
//                                          -> OK icon
//   COMMIT                              -> OK saved     写 NVS + 立刻重画
//   CLEAR                               -> OK defaults  抹掉用户改动，退回出厂值
//   REBOOT                              -> OK rebooting
//   PING                                -> PONG
// ---------------------------------------------------------------------------
static WiFiServer g_cfgServer(CFG_PORT);
static WiFiClient g_cfg;
static String     g_cfgRx;
static File       g_cfgIconFile;
static int        g_cfgIconSlot = -1;      // >=0 表示正在收某个槽的图标字节
static int        g_cfgIconLeft = 0;
static uint32_t   g_cfgIconDeadline = 0;
static uint32_t   g_cfgIdle = 0;           // 上次收到配置字节的时刻（兜底的空闲超时）
// 兜底：配置器要是崩了 / 网线拔了，连接会一直占着唯一那个槽位，后面谁都连不上。
// 30 秒没有任何字节就主动踢掉。（正常一次"读板子 + 应用"也就几秒。）
#define CFG_IDLE_MS 30000

// 二维码那几个函数定义在下面（"二维码"一节），但 8269 的 PROV 指令要用它们，
// 所以先在这里声明一下。g_qr 是画二维码时编码出来的矩阵，PROV 直接把它读出来。
static QRCode  g_qr;
static bool    qrEncode(const char* text);

static void cfgSay(const char* fmt, ...) {
  if (!g_cfg || !g_cfg.connected()) return;
  char buf[160];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  g_cfg.print(buf);
  g_cfg.print('\n');
  Serial.printf("[CFG] -> %s\n", buf);
}

static void cfgHandleLine(const String& line) {
  Serial.printf("[CFG] <- %s\n", line.c_str());
  int sp = line.indexOf(' ');
  String verb = (sp < 0) ? line : line.substring(0, sp);
  String rest = (sp < 0) ? String("") : line.substring(sp + 1);
  verb.toUpperCase();

  if (verb == "PING") { cfgSay("PONG"); return; }

  if (verb == "HELLO") {
    String t = rest; t.trim();
    if (t != LINK_TOKEN) { cfgSay("ERR bad token"); delay(50); g_cfg.stop(); return; }
    cfgSay("OK %s %d", DEVICE_NAME, APP_COUNT);
    return;
  }

  if (verb == "GET") {
    for (int i = 0; i < APP_COUNT; i++) {
      cfgSay("APP %d %u %d %06X %s", i, (unsigned)g_app[i].id,
             g_appIconFs[i] ? 1 : 0, (unsigned)g_app[i].rgb, g_app[i].label);
    }
    cfgSay("END");
    return;
  }

  if (verb == "SET") {
    // SET <i> <id> <rrggbb> <label...>   —— label 可以带空格，取剩下的全部
    int p1 = rest.indexOf(' ');
    int p2 = (p1 < 0) ? -1 : rest.indexOf(' ', p1 + 1);
    int p3 = (p2 < 0) ? -1 : rest.indexOf(' ', p2 + 1);
    if (p3 < 0) { cfgSay("ERR args"); return; }
    int i = rest.substring(0, p1).toInt();
    int nid = rest.substring(p1 + 1, p2).toInt();
    uint32_t rgb = strtoul(rest.substring(p2 + 1, p3).c_str(), nullptr, 16);
    String lab = rest.substring(p3 + 1);
    lab.trim();
    if (i < 0 || i >= APP_COUNT || nid < 1 || nid > 255) { cfgSay("ERR range"); return; }
    g_app[i].id  = (uint8_t)nid;
    g_app[i].rgb = rgb & 0xFFFFFF;
    lab.toCharArray(g_appLabel[i], APP_LABEL_MAX + 1);
    appSanitize(g_appLabel[i], sizeof(g_appLabel[i]));
    g_app[i].label = g_appLabel[i];
    cfgSay("OK");
    return;
  }

  if (verb == "ICON") {
    int p1 = rest.indexOf(' ');
    if (p1 < 0) { cfgSay("ERR args"); return; }
    int i = rest.substring(0, p1).toInt();
    int n = rest.substring(p1 + 1).toInt();
    if (i < 0 || i >= APP_COUNT || n != ICON_BYTES) { cfgSay("ERR size"); return; }
    if (!g_fsOk) { cfgSay("ERR no fs"); return; }
    char path[24];
    appIconPath(i, path, sizeof(path));
    g_cfgIconFile = SPIFFS.open(path, "w");
    if (!g_cfgIconFile) { cfgSay("ERR fs open"); return; }
    g_cfgIconSlot = i;
    g_cfgIconLeft = n;
    g_cfgIconDeadline = millis() + 15000;
    cfgSay("READY");
    return;
  }

  if (verb == "GICON") {
    // GICON <i>  —— 把板子上第 i 格**当前真正用的**图标位图读回去。
    // 为什么需要：8269 协议原本只能写不能读图标，于是"配置器显示的是本机 exe 抠的图、
    // 板子上却是另一张"这种不一致，从电脑这边根本看不出来。加了这个就能逐字节对。
    int i = rest.toInt();
    if (i < 0 || i >= APP_COUNT) { cfgSay("ERR range"); return; }
    const uint16_t* px = appIcon(i);           // SPIFFS 里有就用 SPIFFS 的，否则 ICONS[i]
    cfgSay("OK %d", ICON_BYTES);
    g_cfg.write((const uint8_t*)px, ICON_BYTES);
    g_cfg.flush();
    g_cfgIdle = millis();
    return;
  }

  if (verb == "PROV") {
    // 配网状态 + 二维码矩阵。为什么要把矩阵也吐出来：二维码画在屏幕上是"只写"的，
    // 电脑这边看不见，万一编错了（版本不对 / 数据截断 / 静默区不够）我也无从发现。
    // 把矩阵读回来就能在电脑上重建成图、和标准编码器逐格对。
    String want = rest; want.trim(); want.toUpperCase();

    if (want == "WEB") {
      // 自己 GET 自己托管的配置页。射频那头我验不了（板子扫不到自己发的热点，
      // 电脑的 netsh 又被 Windows 的"位置"权限挡住），但"网页到底能不能正常吐出来"
      // 是可以在板上闭环验的：连 192.168.4.1:80 发一次真实请求，看状态行和长度。
      // 手机扫码之后能不能配网，坏就坏在这几步上，所以值得单独测。
      const char* paths[] = { "/", "/generate_204", "/hotspot-detect.html" };
      for (uint8_t k = 0; k < 3; k++) {
        WiFiClient c;
        if (!c.connect(WiFi.softAPIP(), PROV_HTTP_PORT)) {
          cfgSay("WEB %s CONNECT FAILED", paths[k]);
          continue;
        }
        c.printf("GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n",
                 paths[k], WiFi.softAPIP().toString().c_str());
        uint32_t t0 = millis();
        String status, len, loc;
        int total = 0;
        while ((c.connected() || c.available()) && millis() - t0 < 2500) {
          // 【关键】必须在这儿手动泵一次服务器。cfgHandleLine 是从 loop() -> pollCfg()
          // 进来的，我们在这条路上阻塞等待，loop() 就转不动，provPoll() 也就永远
          // 不会调用 handleClient() —— 请求是我们自己发的，却没人应答，
          // 必然超时。第一次就是这么写的，三条全是 "(no status) 0 bytes"。
          g_provHttp.handleClient();
          if (!c.available()) { delay(2); continue; }
          String ln = c.readStringUntil('\n');
          total += ln.length();
          ln.trim();
          if (status.length() == 0)      status = ln;             // 状态行
          else if (ln.startsWith("Content-Length:")) len = ln;
          else if (ln.startsWith("Location:"))       loc = ln;
          if (ln.length() == 0) {                                  // 空行 = 头结束
            while (c.available()) { c.read(); total++; }            // 正文抽干好让 total 准
            break;
          }
        }
        c.stop();
        cfgSay("WEB %s -> %s  %s %s  %d bytes  (%lums)",
               paths[k], status.length() ? status.c_str() : "(no status)",
               len.c_str(), loc.c_str(), total, (unsigned long)(millis() - t0));
      }
      cfgSay("END");
      return;
    }

    if (want == "SCAN") {
      // 自证：让板子扫一遍自己发的热点。AP 到底有没有真的在发信标，从板子自己的
      // 状态里看不出来（softAPIP() 就算 AP 起不来也照回 192.168.4.1），
      // 但"扫得到自己"是硬证据。顺带把扫到的信道报出来 —— STA 和 AP 抢一个射频，
      // AP 会被拽到 STA 的信道上，这行能看出它落在哪。
      cfgSay("scanning...");
      int n = WiFi.scanNetworks();
      int hit = -1;
      for (int i = 0; i < n; i++) {
        if (WiFi.SSID(i) == g_provSsid) { hit = i; break; }
      }
      cfgSay("SCAN found=%d of=%d self=%s", n, hit >= 0 ? 1 : 0,
             hit >= 0 ? "yes" : "NO");
      if (hit >= 0) {
        cfgSay("SELF rssi=%d ch=%d bssid=%s", (int)WiFi.RSSI(hit),
               (int)WiFi.channel(hit), WiFi.BSSIDstr(hit).c_str());
      }
      // AP_STA 共用一个射频，AP 必然跟 STA 同信道；上一条 SELF 里的 ch 就是 AP 的信道。
      cfgSay("STA ch=%d rssi=%d ip=%s", (int)WiFi.channel(), (int)WiFi.RSSI(),
             WiFi.localIP().toString().c_str());
      cfgSay("AP ip=%s mac=%s stations=%d",
             WiFi.softAPIP().toString().c_str(),
             WiFi.softAPmacAddress().c_str(), (int)WiFi.softAPgetStationNum());
      WiFi.scanDelete();
      cfgSay("END");
      return;
    }

    cfgSay("PROV ap=%d clients=%d ssid=%s pass=%s url=http://%s/ cred=%s",
           (int)g_provOn, (int)(g_provOn ? WiFi.softAPgetStationNum() : 0),
           g_provSsid.c_str(), PROV_AP_PASS,
           WiFi.softAPIP().toString().c_str(),
           g_provHaveCred ? "nvs" : "secrets");
    cfgSay("QRTEXT %s", provQrText().c_str());
    if (want == "QR") {
      // 注意这里不看 g_provOn：热点连上 WiFi 半分钟后会自动关，但那只是省信道，
      // 二维码该编还是编得出来。不加这个判断的话，_qr_check.py 就只能在开机那
      // 半分钟里跑得通 —— 一个诊断工具依赖被诊断对象的状态，本身就说不通。
      if (qrEncode(provQrText().c_str())) {
        cfgSay("QRMATRIX %d %u", (int)g_qr.size, (unsigned)g_qr.version);
        for (int y = 0; y < g_qr.size; y++) {
          String row;
          row.reserve(g_qr.size + 1);
          for (int x = 0; x < g_qr.size; x++)
            row += qrcode_getModule(&g_qr, x, y) ? '1' : '0';
          cfgSay("%s", row.c_str());
        }
      } else {
        cfgSay("ERR qr encode failed");
      }
    }
    cfgSay("END");
    return;
  }

  if (verb == "COMMIT") {
    bool ok = appStoreSave();
    drawGrid();
    cfgSay("%s", ok ? "OK saved" : "ERR nvs");
    Serial.println("[CFG] committed -> grid redrawn");
    return;
  }

  if (verb == "CLEAR") {
    appStoreReset();
    drawGrid();
    cfgSay("OK defaults");
    Serial.println("[CFG] cleared -> factory defaults");
    return;
  }

  if (verb == "REBOOT") {
    cfgSay("OK rebooting");
    delay(150);
    ESP.restart();
    return;
  }

  if (verb == "BYE") {
    // 配置器干完活主动道别。为什么需要这个：TCP 对端 close() 之后，板子这边
    // 要等到下一次 recv 才发现 FIN，这中间新连接会被晾在 backlog 里、最后被 RST。
    // 有了 BYE，"读板子 -> 应用 -> 再读板子"这种连续操作就不会偶发连不上了。
    cfgSay("OK bye");
    g_cfg.flush();          // 必须 flush：stop() 一关，还在缓冲里的 "OK bye" 就发不出去了
    delay(50);              // 再给它一点时间真的出网卡（这 50ms 换来的是稳定的可控关闭）
    g_cfg.stop();
    return;
  }
  cfgSay("ERR unknown");
}

static void pollCfg() {
  if (g_cfg && !g_cfg.connected()) {
    if (g_cfgIconSlot >= 0) { g_cfgIconFile.close(); g_cfgIconSlot = -1; g_cfgIconLeft = 0; }
    g_cfg.stop();
    g_cfgRx = "";
    Serial.println("[CFG] client gone");
  }
  if (WiFi.status() != WL_CONNECTED) return;

  if (!g_cfg || !g_cfg.connected()) {
    WiFiClient nc = g_cfgServer.available();
    if (nc) {
      g_cfg = nc;
      g_cfg.setNoDelay(true);
      g_cfgRx = "";
      g_cfgIconSlot = -1;
      g_cfgIdle = millis();
      Serial.printf("[CFG] client %s\n", g_cfg.remoteIP().toString().c_str());
    }
  }
  if (!g_cfg || !g_cfg.connected()) return;

  // ---- 兜底的空闲超时：占着槽位不说话就踢掉 ----
  if (g_cfgIconSlot < 0 && (uint32_t)(millis() - g_cfgIdle) > CFG_IDLE_MS) {
    Serial.println("[CFG] idle timeout, dropping client");
    g_cfg.stop();
    g_cfgRx = "";
    return;
  }

  // ---- 收图标：裸字节，这段时间不解析文本行 ----
  if (g_cfgIconSlot >= 0) {
    static uint8_t chunk[1024];
    while (g_cfgIconLeft > 0 && g_cfg.available()) {
      int want = (g_cfgIconLeft < (int)sizeof(chunk)) ? g_cfgIconLeft : (int)sizeof(chunk);
      int got  = g_cfg.read(chunk, want);
      if (got <= 0) break;
      g_cfgIconFile.write(chunk, got);
      g_cfgIconLeft -= got;
      g_cfgIdle = millis();
    }
    if (g_cfgIconLeft == 0) {
      g_cfgIconFile.close();
      g_appIconFs[g_cfgIconSlot] = true;
      cfgSay("OK icon");
      Serial.printf("[CFG] icon %d stored (%d bytes)\n", g_cfgIconSlot, ICON_BYTES);
      g_cfgIconSlot = -1;
    } else if ((int32_t)(millis() - g_cfgIconDeadline) > 0) {
      g_cfgIconFile.close();
      char path[24];
      appIconPath(g_cfgIconSlot, path, sizeof(path));
      SPIFFS.remove(path);
      Serial.printf("[CFG] icon %d timed out\n", g_cfgIconSlot);
      g_cfgIconSlot = -1;
      cfgSay("ERR icon timeout");
    }
    return;
  }

  // ---- 收文本行 ----
  if (g_cfg.available()) g_cfgIdle = millis();
  while (g_cfg.available()) {
    char c = (char)g_cfg.read();
    if (c == '\n') {
      String l = g_cfgRx;
      g_cfgRx = "";
      l.trim();
      if (l.length()) cfgHandleLine(l);
      if (!g_cfg || !g_cfg.connected()) break;   // 上一行可能已经把连接踢了
      if (g_cfgIconSlot >= 0) break;             // 接下来是图标裸字节，别再当文本读
    } else if (c != '\r' && g_cfgRx.length() < 200) {
      g_cfgRx += c;
    }
  }
}

// 见 g_bootScreen 附近的说明。SELFTEST_MS 之后自动切到正常界面；
// 想再看一次就拔插一次 USB（开串口也会复位，效果一样）。
// 在这段时间里点屏幕会换朝向、按住会开关反色（见 pollDispSetup），
// 每次操作都把倒计时推后，所以可以慢慢试到自己满意为止。
#define SELFTEST_MS 20000

// ---------------------------------------------------------------------------
// 二维码
// ---------------------------------------------------------------------------
// 拆成三步是因为位置要先知道尺寸才能算：编码 -> 量边 -> 画。
// 屏幕是 320x240 这种尺寸，二维码边长直接决定左边还能留多少地方给方向自检，
// 所以必须"先量后摆"。
static uint8_t g_qrBuf[1024];          // 够到版本 15（77x77 需要 742 字节）；g_qr 本身声明在上面

// 字节模式下各版本能装的字符数（ECC L）。这张表必须自己维护，原因见 qrEncode。
static uint16_t qrByteCapacity(uint8_t ver) {
  static const uint16_t cap[16] = {
    0,                                          // 版本 0 不存在
    17, 32, 53, 78, 106, 134, 154, 192,         // v1..v8
    230, 271, 321, 367, 425, 458, 520,          // v9..v15
  };
  return (ver <= 15) ? cap[ver] : 0;
}

static bool qrEncode(const char* text) {
  // 【别改回"靠返回值判断装不装得下"】ricmoo/QRCode 的 qrcode_initBytes 只在
  // encodeDataCodewords 返回负数时才报错，而那个函数三种模式全都无条件 return 正数；
  // 它底下的 bb_appendBits 又完全不检查容量，超长时直接往定长栈数组外面写。
  // 实测：47 字节的 WIFI 串配 version 2（codewordBytes 只有 45 字节）——
  // 返回 0（"成功"）、编出一张解不开的图、还踩了 4 字节栈。
  // 所以版本一律按容量表自己挑。
  size_t len = strlen(text);
  for (uint8_t ver = 2; ver <= 15; ver++) {
    if (qrByteCapacity(ver) < len) continue;   // 这个版本装不下，继续往上找
    if (qrcode_getBufferSize(ver) > sizeof(g_qrBuf)) break;
    if (qrcode_initText(&g_qr, g_qrBuf, ver, ECC_LOW, text) == 0) {
      Serial.printf("[QR] v%u %dx%d, %u 字节\n",
                    (unsigned)ver, (int)g_qr.size, (int)g_qr.size, (unsigned)len);
      return true;
    }
  }
  Serial.printf("[QR] 编不出来（%u 字节，超出 v15 的 %u）：%s\n",
                (unsigned)len, (unsigned)qrByteCapacity(15), text);
  return false;
}

// 含白边的实际边长
static int qrSidePx(int px, int quiet) { return (g_qr.size + quiet * 2) * px; }

// 白边按规范要 4 个模块，但屏幕上 2 个就够 —— 屏幕本身在深色底上，
// 对比度比印在纸上好得多，实测手机秒扫。
static void drawQRAt(int x, int y, int px, int quiet) {
  const int n = g_qr.size;
  const int total = qrSidePx(px, quiet);
  tft.fillRect(x, y, total, total, 0xFFFFFF);
  // 逐行把连续的模块并成一段再 fillRect：29x29 有 841 个模块，一个一个画要几百次
  // SPI 事务，合并之后调用次数砍掉一半以上，开机画面不至于卡。
  for (int my = 0; my < n; my++) {
    int mx = 0;
    while (mx < n) {
      if (!qrcode_getModule(&g_qr, mx, my)) { mx++; continue; }
      int start = mx;
      while (mx < n && qrcode_getModule(&g_qr, mx, my)) mx++;
      tft.fillRect(x + (start + quiet) * px, y + (my + quiet) * px,
                   (mx - start) * px, px, 0x000000);
    }
  }
  Serial.printf("[QR] v%u %dx%d modules, %dpx/module -> %dx%d px at (%d,%d)\n",
                (unsigned)g_qr.version, n, n, px, total, total, x, y);
}

static void drawSelfTest() {
  tft.fillScreen(C_BG);

  // 整屏边框：两头都要看得见，说明一个像素都没漏掉
  tft.drawRect(0, 0, g_sw - 1, g_sh - 1, 0xFFFFFF);
  tft.drawRect(1, 1, g_sw - 3, g_sh - 3, 0x404040);

  tft.setFont(&lgfx::fonts::Font2);
  tft.setTextDatum(middle_center);

  const bool land  = (g_sw >= g_sh);
  const int  quiet = 2;

  // ---- 配网二维码 ----
  // 放在右侧（横屏）或顶部（竖屏），方向自检的内容挤到剩下的地方。
  // 顺带一提：二维码本身就是最好的方向判据 —— 只有摆正了手机才扫得出来，
  // 比读英文字母直观得多。
  int qx = 0, qy = 0, qside = 0, qpx = 0;
  bool qrOk = false;
  if (g_provOn && qrEncode(provQrText().c_str())) {
    const int maxW = land ? (g_sw * 5) / 8 : (g_sw - 12);
    const int maxH = land ? (g_sh - 52)  : (g_sh / 2);
    for (int cand = 5; cand >= 2; cand--) {
      int s = qrSidePx(cand, quiet);
      if (s <= maxW && s <= maxH) { qside = s; qpx = cand; qrOk = true; break; }
    }
    if (qrOk) {
      qx = land ? (g_sw - 6 - qside) : ((g_sw - qside) / 2);
      qy = land ? 16 : 14;
      drawQRAt(qx, qy, qpx, quiet);
    }
  }

  // 左栏（横屏）或下半屏（竖屏）—— 方向自检
  const int capY = qrOk ? (qy + qside + 13) : 0;   // 二维码下面第一行说明的 y
  const int cx   = land ? ((qrOk ? qx : g_sw) - 6) / 2 : g_sw / 2;
  const int top  = land ? 6 : (qrOk ? capY + 42 : 14);

  // 大向上箭头：不需要读英文也能判断朝向 —— 箭头指向哪边，就点一下换下一种，
  // 直到它朝上（同时文字也能一眼读通）。这块屏只能写不能读，方向对不对只有肉眼
  // 能定，所以判据必须做成"不需要文字"的。
  tft.fillTriangle(cx, top, cx - 20, top + 24, cx + 20, top + 24, 0x58A6FF);
  tft.setTextColor(0xFFFFFF, C_BG);
  tft.drawString("TAP = rotate", cx, top + 40);
  tft.setTextColor(0xF0B90B, C_BG);
  tft.drawString("HOLD = invert", cx, top + 58);

  // ---- 当前生效的参数（运行时值，不是编译期常数）----
  {
    char buf[40];
    snprintf(buf, sizeof(buf), "%dx%d r%u o%u v%u", g_sw, g_sh,
             (unsigned)g_dispRot, (unsigned)g_dispOff, (unsigned)g_dispInv);
    tft.setFont(&lgfx::fonts::Font0);
    tft.setTextColor(0x3FB950, C_BG);
    tft.drawString(buf, cx, top + 76);
    tft.setFont(&lgfx::fonts::Font2);
  }

  // ---- 黑白对照 + RGB 顺序对照：只在横屏画 ----
  // 竖屏时二维码吃掉上半屏，剩下的高度放不下，硬画会跑出屏幕。
  if (land) {
    const int bw = 56, bh = 32;
    const int by = top + 94;
    tft.fillRect(cx - bw - 4, by, bw, bh, 0xFFFFFF);
    tft.fillRect(cx + 4,      by, bw, bh, 0x000000);
    tft.drawRect(cx - bw - 4, by, bw, bh, 0x808080);
    tft.drawRect(cx + 4,      by, bw, bh, 0x808080);
    tft.setFont(&lgfx::fonts::Font0);
    tft.setTextColor(0xFFFFFF, C_BG);
    tft.drawString("WHITE", cx - bw / 2 - 4, by + bh + 10);
    tft.drawString("BLACK", cx + bw / 2 + 4, by + bh + 10);

    static const uint32_t swatch[6] = {0xFF0000, 0x00FF00, 0x0000FF,
                                       0xFFFF00, 0x00FFFF, 0xFF00FF};
    const int s = 16, gap = 3;
    const int sy = 172;
    for (int i = 0; i < 6; i++)
      tft.fillRect(cx - (6 * s + 5 * gap) / 2 + i * (s + gap), sy, s, s, swatch[i]);
    tft.setTextColor(0x7D8590, C_BG);
    tft.drawString("R G B Y C M", cx, sy - 9);

    // 强制门户没自动弹出来时，就得手动敲这个地址
    tft.setTextColor(0x58A6FF, C_BG);
    tft.drawString(String("open http://") + WiFi.softAPIP().toString(), cx, 204);
    tft.setFont(&lgfx::fonts::Font2);
  }

  // ---- 上下左右判据 ----
  // 底边一行：LEFT / BOTTOM / RIGHT。二维码占了右边，所以这几行都压在左栏和底边上，
  // 不能像以前那样直接把 RIGHT 贴在屏幕右缘的中间。
  tft.setTextColor(0x58A6FF, C_BG);
  tft.drawString("BOTTOM", cx, g_sh - 8);
  tft.setFont(&lgfx::fonts::Font0);
  tft.setTextDatum(middle_left);
  tft.drawString("LEFT", 4, g_sh - 8);
  tft.setTextDatum(middle_right);
  tft.drawString("RIGHT", g_sw - 4, g_sh - 8);
  tft.setTextDatum(middle_center);

  // ---- 二维码下面的说明：手机扫的就是上面那张码 ----
  if (qrOk) {
    const int ccx = qx + qside / 2;
    tft.setFont(&lgfx::fonts::Font2);
    tft.setTextColor(0xFFFFFF, C_BG);
    tft.drawString("SCAN TO SET UP", ccx, capY);
    tft.setFont(&lgfx::fonts::Font0);
    tft.setTextColor(0x3FB950, C_BG);
    tft.drawString(g_provSsid.c_str(), ccx, capY + 15);
    tft.setTextColor(0x7D8590, C_BG);
    tft.drawString(String("pass ") + PROV_AP_PASS, ccx, capY + 26);
  }
  tft.setFont(&lgfx::fonts::Font2);
}

// 自检画面期间的手势：点一下 = 换一种朝向，按住 = 反色开关。
//
// 不用轮询式长按判定（那样要点满 1.2 秒才反应，手感很差）：按下先记时间，
// 一旦超过阈值就当场触发反色并标记 holdFired，松手时就不会再当成"点一下"。
static bool readTouch(int32_t &tx, int32_t &ty);      // 定义在下面

static void pollDispSetup() {
  static bool     down = false;
  static uint32_t downAt = 0;
  static bool     holdFired = false;

  int32_t tx = 0, ty = 0;
  bool now = readTouch(tx, ty);

  if (now && !down) {
    down = true;
    downAt = millis();
    holdFired = false;
  } else if (now && down) {
    if (!holdFired && (uint32_t)(millis() - downAt) >= 1200) {
      holdFired = true;
      g_dispInv = !g_dispInv;
      applyDisplay();
      dispSaveNvs();
      drawSelfTest();
      g_bootAt = millis();               // 操作过就把倒计时推后
      Serial.printf("[DISP] invert -> %u (RAM only, reboot restores the compile-time value)\n",
                    (unsigned)g_dispInv);
    }
  } else if (!now && down) {
    down = false;
    if (!holdFired) {
      g_dispOff = (uint8_t)((g_dispOff + 1) & 7);
      applyDisplay();
      dispSaveNvs();
      drawSelfTest();
      g_bootAt = millis();
      Serial.printf("[DISP] offset_rotation -> %u (RAM only, reboot restores the compile-time value)\n",
                    (unsigned)g_dispOff);
    }
  }
}

// ---------------------------------------------------------------------------
// 和 PC 说话
// ---------------------------------------------------------------------------
static void sendLine(const char* s) {
  if (g_client && g_client.connected()) {
    g_client.print(s);
    g_client.print('\n');
    Serial.printf("[TCP] -> %s\n", s);
  }
}

static void launchApp(int i) {
  if (i < 0 || i >= APP_COUNT) return;
  TileRuntime& rt = g_tile[i];
  if (rt.state == TS_PENDING) return;             // 已经点过了，别重复发

  if (g_link != 2) {
    rt.state = TS_ERR;
    rt.until = millis() + RESULT_SHOW_MS;
    postMsg("PC offline");
    drawTile(i);
    drawStatusBar();
    return;
  }
  char buf[24];
  snprintf(buf, sizeof(buf), "LAUNCH %u", (unsigned)g_app[i].id);
  sendLine(buf);
  rt.state = TS_PENDING;
  rt.until = millis() + 15000;                   // 15 秒没回复就算超时
  postMsg("Launching %s", g_app[i].label);
  drawTile(i);
  drawStatusBar();
}

// ---------------------------------------------------------------------------
// 协议解析（PC -> ESP32）
// ---------------------------------------------------------------------------
static void handleLine(const String& line) {
  if (line.length() == 0) return;

  if (g_link == 1) {                              // 等握手
    if (line.startsWith("HELLO ")) {
      String rest = line.substring(6);
      int sp = rest.indexOf(' ');
      String token = (sp < 0) ? rest : rest.substring(0, sp);
      if (token == LINK_TOKEN) {
        g_link = 2;
        g_client.printf("WELCOME %s\n", DEVICE_NAME);
        Serial.println("[TCP] handshake OK");
        postMsg("PC connected");
      } else {
        Serial.println("[TCP] bad token, dropping");
        g_client.stop();
        g_link = 0;
      }
    }
    drawStatusBar();
    return;
  }

  int sp = line.indexOf(' ');
  String verb = (sp < 0) ? line : line.substring(0, sp);
  String rest = (sp < 0) ? "" : line.substring(sp + 1);
  verb.toUpperCase();

  if (verb == "PING") {
    sendLine("PONG");
  } else if (verb == "OK" || verb == "ERR") {
    int id = rest.toInt();
    for (int i = 0; i < APP_COUNT; i++) {
      if (g_app[i].id != id) continue;
      g_tile[i].state = (verb == "OK") ? TS_OK : TS_ERR;
      g_tile[i].until = millis() + RESULT_SHOW_MS;
      postMsg("%s %s", verb == "OK" ? "Done:" : "Fail:", g_app[i].label);
      drawTile(i);
      break;
    }
  }
}

static void pollClient() {
  if (g_link > 0 && !g_client.connected()) {
    g_client.stop();
    g_link = 0;
    g_rx = "";
    Serial.println("[TCP] disconnected");
    drawStatusBar();
  }
  if (g_link == 1 && (int32_t)(millis() - g_helloDeadline) > 0) {
    Serial.println("[TCP] handshake timeout");
    g_client.stop();
    g_link = 0;
    drawStatusBar();
  }

  if (WiFi.status() != WL_CONNECTED) return;

  if (g_link == 0) {
    WiFiClient nc = g_server.available();
    if (nc) {
      g_client = nc;
      g_client.setNoDelay(true);
      g_link = 1;
      g_helloDeadline = millis() + 3000;
      g_rx = "";
      Serial.printf("[TCP] client %s\n", g_client.remoteIP().toString().c_str());
      drawStatusBar();
    }
  }

  if (g_client && g_client.connected()) {
    while (g_client.available()) {
      char c = (char)g_client.read();
      g_lastRx = millis();
      if (c == '\n') {
        String line = g_rx;
        g_rx = "";
        line.trim();
        handleLine(line);
      } else if (c != '\r' && g_rx.length() < 160) {
        g_rx += c;
      }
    }
    if (g_link == 2 && millis() - g_lastPing > 20000) {
      g_lastPing = millis();
      sendLine("PING");
    }
  }
}

static void pollUdp() {
  int sz = g_udp.parsePacket();
  if (sz <= 0) return;
  char buf[32] = {0};
  int n = g_udp.read(buf, sizeof(buf) - 1);
  if (n <= 0) return;
  if (strncmp(buf, "ESP32LAUNCHER?", 14) == 0) {
    char reply[64];
    snprintf(reply, sizeof(reply), "ESP32LAUNCHER:%s:%d", DEVICE_NAME, TCP_PORT);
    g_udp.beginPacket(g_udp.remoteIP(), g_udp.remotePort());
    g_udp.write((const uint8_t*)reply, strlen(reply));
    g_udp.endPacket();
    Serial.printf("[UDP] discovery from %s\n", g_udp.remoteIP().toString().c_str());
  }
}

// ---------------------------------------------------------------------------
// 触摸标定向导
//
//  为什么要做：XPT2046 是电阻屏，ADC 的两个端点每块板子都不一样，编译期写死的
//  值只能算个起点；Y 方向如果反了，就会出现"点上面那格、结果开了下面那个程序"。
//  这里让用户在屏幕上点两个靶心，把实测的原始 ADC 值反算成新的
//  x_min/x_max/y_min/y_max 存进 NVS，下次开机自动生效 —— 不用改代码、不用重烧。
//
//  怎么进：串口发 cal，或者按住任意一张卡片 5 秒。再发一次 cal 就取消。
// ---------------------------------------------------------------------------
#define NVS_CAL_NS "tcal"

enum CalStep : uint8_t { CAL_OFF = 0, CAL_TAP1, CAL_TAP2, CAL_VERIFY, CAL_DONE };

static CalStep  g_cal = CAL_OFF;
static uint32_t g_calUntil = 0;        // CAL_DONE 画面展示到什么时候
static int      g_calTarget[2][2];     // 两个靶心的屏幕坐标
static int      g_calRaw[2][2];        // 在两个靶心上采到的原始 ADC 值
static int      g_calNew[4] = {0};     // 算出来的新标定值
static uint16_t g_calOld[4] = {0};     // 老值，验证不过就回滚
static const int CAL_R = 16;           // 靶心半径
#define CAL_HOLD_MS 5000               // 按住卡片多久算"我要标定"

static void calShowStep();

static void calDrawTarget(int cx, int cy, uint32_t color) {
  tft.fillCircle(cx, cy, CAL_R, color);
  tft.drawCircle(cx, cy, CAL_R + 7, C_TEXT);
  tft.drawFastHLine(cx - CAL_R - 24, cy, 12, C_TEXT);
  tft.drawFastHLine(cx + CAL_R + 12, cy, 12, C_TEXT);
  tft.drawFastVLine(cx, cy - CAL_R - 24, 12, C_TEXT);
  tft.drawFastVLine(cx, cy + CAL_R + 12, 12, C_TEXT);
}

static void calShowStep() {
  tft.fillScreen(C_BG);
  tft.setTextDatum(top_center);
  tft.setFont(&lgfx::fonts::Font2);

  const char* msg;
  int ti;
  switch (g_cal) {
    case CAL_TAP1:   msg = "CAL 1/2 - tap the dot"; ti = 0; break;
    case CAL_TAP2:   msg = "CAL 2/2 - tap the dot"; ti = 1; break;
    case CAL_VERIFY: msg = "Check - tap the dot";   ti = 2; break;
    default: return;
  }
  tft.setTextColor(C_TEXT, C_BG);
  tft.drawString(msg, g_sw / 2, 12);

  if (ti < 2) calDrawTarget(g_calTarget[ti][0], g_calTarget[ti][1], C_WARN);
  else        calDrawTarget(g_sw / 2, g_sh / 2, C_OK);

  tft.setFont(&lgfx::fonts::Font0);
  tft.setTextColor(C_DIM, C_BG);
  tft.setTextDatum(bottom_center);
  tft.drawString("serial: cal = cancel", g_sw / 2, g_sh - 6);
}

static void calSaveNvs() {
  Preferences p;
  if (!p.begin(NVS_CAL_NS, false)) { Serial.println("[CAL] NVS open failed"); return; }
  p.putUShort("xmin", (uint16_t)g_calNew[0]);
  p.putUShort("xmax", (uint16_t)g_calNew[1]);
  p.putUShort("ymin", (uint16_t)g_calNew[2]);
  p.putUShort("ymax", (uint16_t)g_calNew[3]);
  p.end();
  Serial.println("[CAL] saved to NVS");
}

// 开机时把上次标定的结果读回来（没有就用编译期默认值）
static void calLoadNvs() {
  Preferences p;
  // 用 readOnly=false：命名空间还不存在时不会报 nvs_open failed: NOT_FOUND
  if (!p.begin(NVS_CAL_NS, false)) return;
  bool have = p.isKey("xmin") && p.isKey("xmax") && p.isKey("ymin") && p.isKey("ymax");
  uint16_t a = p.getUShort("xmin", 0), b = p.getUShort("xmax", 0);
  uint16_t c = p.getUShort("ymin", 0), d = p.getUShort("ymax", 0);
  p.end();
  if (!have) return;
  // 注意：这里【不能】假设 ymin < ymax —— XPT2046 装在这块屏上时 y 本来就是反的
  if (abs((int)b - (int)a) < 300 || abs((int)d - (int)c) < 300) {
    Serial.println("[CAL] stored values look broken, ignored");
    return;
  }
  tft.setTouchCal(a, b, c, d);
  Serial.printf("[CAL] from NVS: x %u..%u  y %u..%u\n", a, b, c, d);
}

static void calFinish(bool ok, const char* why) {
  if (ok) {
    calSaveNvs();
  } else {
    tft.setTouchCal(g_calOld[0], g_calOld[1], g_calOld[2], g_calOld[3]);
    Serial.printf("[CAL] FAILED: %s -- old values restored\n", why);
  }
  g_cal = CAL_DONE;
  g_calUntil = millis() + 2600;

  tft.fillScreen(C_BG);
  tft.setTextDatum(middle_center);
  tft.setFont(&lgfx::fonts::Font2);
  tft.setTextColor(ok ? C_OK : C_ERR, C_BG);
  tft.drawString(ok ? "Touch OK" : "Cal failed", g_sw / 2, g_sh / 2 - 26);

  tft.setFont(&lgfx::fonts::Font0);
  tft.setTextColor(C_DIM, C_BG);
  if (ok) {
    char buf[48];
    snprintf(buf, sizeof(buf), "x %d..%d   y %d..%d",
             g_calNew[0], g_calNew[1], g_calNew[2], g_calNew[3]);
    tft.drawString("saved (no reflash needed)", g_sw / 2, g_sh / 2 - 4);
    tft.drawString(buf, g_sw / 2, g_sh / 2 + 12);
  } else {
    tft.drawString(why, g_sw / 2, g_sh / 2 - 4);
    tft.drawString("old settings kept", g_sw / 2, g_sh / 2 + 12);
  }
}

// 把两个靶心的实测原始值反解成 x_min/x_max/y_min/y_max
static void calCompute() {
  // 标定值是定义在"面板原始方向"上的，而用户是在旋转后的屏幕上点的，
  // 所以先把两个靶心的【屏幕】坐标换算回【面板】坐标。
  int p1x, p1y, p2x, p2y;
  tft.screenToPanelSpace(g_calTarget[0][0], g_calTarget[0][1], p1x, p1y);
  tft.screenToPanelSpace(g_calTarget[1][0], g_calTarget[1][1], p2x, p2y);

  Serial.printf("[CAL] target screen (%d,%d)/(%d,%d) -> panel (%d,%d)/(%d,%d)\n",
                g_calTarget[0][0], g_calTarget[0][1],
                g_calTarget[1][0], g_calTarget[1][1], p1x, p1y, p2x, p2y);
  Serial.printf("[CAL] raw (%d,%d)/(%d,%d)\n",
                g_calRaw[0][0], g_calRaw[0][1], g_calRaw[1][0], g_calRaw[1][1]);

  // 两个靶心必须在每条 ADC 轴上真的分得开，否则解出的斜率只是噪声
  if (abs(g_calRaw[1][0] - g_calRaw[0][0]) < 200 ||
      abs(g_calRaw[1][1] - g_calRaw[0][1]) < 200) {
    calFinish(false, "samples too close");
    return;
  }

  uint16_t xmin, xmax, ymin, ymax;
  tft.solveTouchCal((float)g_calRaw[0][0], (float)p1x, (float)g_calRaw[1][0], (float)p2x,
                    (float)g_calRaw[0][1], (float)p1y, (float)g_calRaw[1][1], (float)p2y,
                    xmin, xmax, ymin, ymax);
  Serial.printf("[CAL] new x %u..%u  y %u..%u\n", xmin, xmax, ymin, ymax);

  // 合理性检查：XPT2046 是 12 位，端点应落在 0..4095；两端也要分得开。
  // 注意【不能】假设 ymin < ymax —— 这块屏的 y 本来就是反的。
  // 另外 xmin 是 uint16_t，lroundf 解出负数会回绕成很大的数，靠 hi>4095 拦下。
  int hi = max(max((int)xmin, (int)xmax), max((int)ymin, (int)ymax));
  bool bad = abs((int)xmax - (int)xmin) < 400 || abs((int)ymax - (int)ymin) < 400
          || hi > 4095;
  if (bad) { calFinish(false, "out of range"); return; }

  g_calNew[0] = xmin; g_calNew[1] = xmax; g_calNew[2] = ymin; g_calNew[3] = ymax;
  tft.setTouchCal(xmin, xmax, ymin, ymax);
  g_cal = CAL_VERIFY;
  calShowStep();
}

static void calStart() {
  if (g_cal != CAL_OFF) {                       // 再喊一次 = 取消
    tft.setTouchCal(g_calOld[0], g_calOld[1], g_calOld[2], g_calOld[3]);
    g_cal = CAL_OFF;
    Serial.println("[CAL] cancelled");
    drawGrid();
    return;
  }
  tft.getTouchCal(g_calOld[0], g_calOld[1], g_calOld[2], g_calOld[3]);
  g_calTarget[0][0] = 26;         g_calTarget[0][1] = 26;
  g_calTarget[1][0] = g_sw - 27;  g_calTarget[1][1] = g_sh - 27;
  g_cal = CAL_TAP1;
  Serial.printf("[CAL] start; old x %u..%u  y %u..%u  irq=%d\n",
                g_calOld[0], g_calOld[1], g_calOld[2], g_calOld[3],
                tft.touchIrqEnabled() ? 1 : 0);
  calShowStep();
}

static void pollCal() {
  static bool     was = false;
  static uint32_t n = 0;
  static int32_t  accRx = 0, accRy = 0;   // 原始 ADC 值累加
  static int32_t  accSx = 0, accSy = 0;   // 标定后屏幕坐标累加

  if (g_cal == CAL_OFF) { was = false; return; }

  if (g_cal == CAL_DONE) {
    if ((int32_t)(millis() - g_calUntil) > 0) { g_cal = CAL_OFF; drawGrid(); }
    return;
  }

  int32_t sx = 0, sy = 0;
  bool now;
  lgfx::touch_point_t rp;
  bool haveRaw;
  if (g_synthRawOn) {
    // 自测模式：喂进一对裸 ADC，屏幕坐标用【当前标定】反算 —— 和真手指完全等价
    now     = ((int32_t)(millis() - g_synthRawUntil) < 0);
    rp.x    = (int16_t)g_synthRawX;
    rp.y    = (int16_t)g_synthRawY;
    rp.size = 400;
    rp.id   = 0;
    haveRaw = true;
    lgfx::touch_point_t sp = rp;
    tft.convertRawXY(&sp, 1);        // public：raw -> 当前标定下的屏幕坐标
    sx = sp.x; sy = sp.y;
    if (!now) g_synthRawOn = false;
  } else {
    now     = tft.getTouch(&sx, &sy);
    haveRaw = tft.getTouchRaw(&rp);
  }

  if (now && !was) { n = 0; accRx = accRy = accSx = accSy = 0; }
  if (haveRaw) { accRx += rp.x; accRy += rp.y; n++; }
  if (now)     { accSx += sx;   accSy += sy; }

  if (!now && was) {                       // 手指抬起 -> 这一步采完了
    if (n < 2) {
      Serial.println("[CAL] too few samples, tap again");
      calShowStep();
    } else {
      int rx = (int)(accRx / (int32_t)n);
      int ry = (int)(accRy / (int32_t)n);
      int vx = (int)(accSx / (int32_t)n);
      int vy = (int)(accSy / (int32_t)n);
      Serial.printf("[CAL] step %d: raw(%d,%d) screen(%d,%d) n=%u\n",
                    (int)g_cal, rx, ry, vx, vy, (unsigned)n);

      if (g_cal == CAL_TAP1) {
        g_calRaw[0][0] = rx; g_calRaw[0][1] = ry;
        g_cal = CAL_TAP2;
        calShowStep();
      } else if (g_cal == CAL_TAP2) {
        g_calRaw[1][0] = rx; g_calRaw[1][1] = ry;
        calCompute();
      } else if (g_cal == CAL_VERIFY) {
        char why[40];
        snprintf(why, sizeof(why), "off by %d,%d px", vx - g_sw / 2, vy - g_sh / 2);
        bool ok = abs(vx - g_sw / 2) <= 60 && abs(vy - g_sh / 2) <= 60;
        Serial.printf("[CAL] verify got (%d,%d) want (%d,%d) -> %s\n",
                      vx, vy, g_sw / 2, g_sh / 2, ok ? "OK" : "FAIL");
        calFinish(ok, why);
      }
    }
  }
  was = now;
}

// ---------------------------------------------------------------------------
// 触摸
// ---------------------------------------------------------------------------

// ---- 合成触摸 ------------------------------------------------------------
// 没有手指的时候，让 AI 也能把 pollTouch() 整条链路（命中判定 -> 按下计时 ->
// 抬起判定 -> launchApp）完整走一遍。不能用 'l <id>' 代替：那个是直接调
// launchApp()，跳过了命中判定，正是最容易出错的一段。
static bool     g_synthOn    = false;
static int32_t  g_synthX     = 0;
static int32_t  g_synthY     = 0;
static uint32_t g_synthUntil = 0;

static void synthTap(int x, int y, uint32_t hold_ms) {
  g_synthX = x;  g_synthY = y;
  g_synthOn = true;
  g_synthUntil = millis() + hold_ms;
  Serial.printf("[SYNTH] tap (%d,%d) hold=%ums -> tile=%d\n", x, y, hold_ms, hitTest(x, y));
}

static bool readTouch(int32_t &tx, int32_t &ty) {
#if HAS_TOUCH
  if (g_synthOn) {
    if ((int32_t)(millis() - g_synthUntil) < 0) { tx = g_synthX; ty = g_synthY; return true; }
    g_synthOn = false;
    return false;                    // 这一帧就是"手指抬起"
  }
  return tft.getTouch(&tx, &ty);
#else
  (void)tx; (void)ty;
  return false;
#endif
}

static void pollTouch() {
#if HAS_TOUCH
  static bool was = false;
  static int  idx = -1;
  static uint32_t downAt = 0;
  static bool holdFired = false;

  if (g_cal != CAL_OFF) {          // 标定进行中，卡片不参与
    was = true; idx = -1; holdFired = false;
    g_touchLive = false;
    return;
  }

  // 必须 static！手指抬起那一帧 tft.getTouch() 返回 false 而且**不会写回坐标**
  // （LGFXBase.hpp:1485 `if (index >= count) return 0;`），
  // 若用局部变量，抬起时 tx/ty 会退回 (0,0)，hitTest(0,0) = -1（在状态栏里），
  // 于是 `up == idx` 永远不成立 -> 真实手指点击永远点不开程序。
  static int32_t tx = 0, ty = 0;
  bool now = readTouch(tx, ty);

  if (g_touchDebug && now) {
    Serial.printf("[TOUCH] x=%d y=%d tile=%d\n", (int)tx, (int)ty, hitTest(tx, ty));
  }
  if (g_touchRawDebug) {
    // 校准用：打印触摸芯片的原始 ADC 值（和上面经过标定的坐标不是一回事）
    lgfx::touch_point_t rp;
    if (tft.getTouchRaw(&rp)) {
      static uint32_t lastRaw = 0;
      if (millis() - lastRaw > 120) {
        lastRaw = millis();
        Serial.printf("[RAW] x=%d y=%d size=%u  -> cal x=%d y=%d\n",
                      (int)rp.x, (int)rp.y, (unsigned)rp.size, (int)tx, (int)ty);
      }
    }
  }

  if (now && !was) {
    idx = hitTest(tx, ty);
    downAt = millis();
    holdFired = false;
    g_touchLive = true;
    g_touchLx = (int)tx; g_touchLy = (int)ty; g_touchLtile = idx;
    if (idx >= 0 && g_tile[idx].state != TS_PENDING) {
      g_tile[idx].state = TS_PRESSED;
      drawTile(idx, false);
    }
    drawStatusBar();
  } else if (now && was) {
    // 手指按住期间实时更新状态栏，让用户看得见"它认为你点在哪儿"
    g_touchLx = (int)tx; g_touchLy = (int)ty;
    int cur = hitTest(tx, ty);
    if (cur != g_touchLtile) {
      g_touchLtile = cur;
      if (idx >= 0 && idx != cur) { g_tile[idx].state = TS_IDLE; drawTile(idx, false); }
      if (cur >= 0 && g_tile[cur].state != TS_PENDING) {
        g_tile[cur].state = TS_PRESSED;
        drawTile(cur, false);
      }
      idx = cur;
      drawStatusBar();
    }
    // 长按 5 秒 -> 进触摸标定向导。
    // 这里**故意不要求** idx >= 0：标定偏得厉害时手指可能落不到任何卡片上，
    // 而那恰恰是最需要进标定向导的时候 —— 要是还要求点中卡片，就永远进不去了。
    if (!holdFired && millis() - downAt >= CAL_HOLD_MS) {
      holdFired = true;
      if (idx >= 0) { g_tile[idx].state = TS_IDLE; drawTile(idx, false); }
      Serial.println("[DBG] long press -> touch calibration");
      g_touchLive = false;
      calStart();
      was = now;
      return;
    }
  } else if (!now && was) {
    g_touchLive = false;
    int up = hitTest(tx, ty);
    if (!holdFired) {
      if (idx >= 0 && up == idx && millis() - downAt >= PRESS_HOLD_MS) {
        g_tile[idx].state = TS_IDLE;
        launchApp(idx);
      } else {
        if (idx >= 0) {
          g_tile[idx].state = TS_IDLE;
          drawTile(idx, false);
          if (up != idx)                        postMsg("moved off %s", g_app[idx].label);
          else if (millis() - downAt < PRESS_HOLD_MS)
                                                postMsg("tap too short (%lums)",
                                                        (unsigned long)(millis() - downAt));
        } else {
          // 触摸被读到了，但没落在任何卡片上 —— 排查标定时最关键的一条信息
          postMsg("touch OK at %d,%d  but no tile", (int)tx, (int)ty);
        }
      }
    }
    drawStatusBar();
    idx = -1;
    holdFired = false;
  }
  was = now;
#endif
}

// ---------------------------------------------------------------------------
// 超时 / 动画收尾
// ---------------------------------------------------------------------------
static void pollTimeouts() {
  uint32_t now = millis();
  for (int i = 0; i < APP_COUNT; i++) {
    TileRuntime& rt = g_tile[i];
    if ((rt.state == TS_OK || rt.state == TS_ERR || rt.state == TS_PENDING) &&
        rt.until && (int32_t)(now - rt.until) > 0) {
      rt.state = TS_IDLE;
      rt.until = 0;
      drawTile(i);                 // 整块重画，顺手把角标擦掉
    }
  }
  static uint32_t last = 0;
  if (now - last > 500) { last = now; drawStatusBar(); }
}

// ---------------------------------------------------------------------------
// 串口调试指令
// ---------------------------------------------------------------------------
static void pollSerial() {
#if SERIAL_DEBUG
  static String sbuf;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      sbuf.trim();
      if (sbuf.length()) {
        char cmd = tolower(sbuf[0]);
        String arg = sbuf.substring(1);
        arg.trim();
        if (cmd == '?') {
          Serial.println("[HELP] l <id>  simulate a finger tap on app id 1..6");
          Serial.println("[HELP] l <x> <y> [hold_ms]  synthetic tap at screen coords (full hit-test path)");
          Serial.println("[HELP] l raw <rx> <ry> [hold_ms]  inject raw touch ADC (drives the cal wizard)");
          Serial.println("[HELP] d       read TFT controller ID (RDDID 0x04) -- tells ILI9341 from ST7789");
          Serial.println("[HELP] o       display setup: o p <0-7> panel offset, o r <0-3> rotation,");
          Serial.println("[HELP]         o i invert toggle, o t <0-7> touch offset, o d defaults");
          Serial.println("[HELP]         (display settings are NOT saved -- fix board.h and reflash)");
          Serial.println("[HELP]         (on-screen: TAP=rotate, HOLD=invert)");
          Serial.println("[HELP] t       toggle touch coordinate debug");
          Serial.println("[HELP] r       toggle RAW touch ADC debug (use while calibrating)");
          Serial.println("[HELP] p       toggle raw XPT2046 SPI probe (tells 'dead bus' from 'bad cal')");
          Serial.println("[HELP] c       touch calibration wizard (send again = cancel)");
          Serial.println("[HELP] i       toggle touch IRQ pin");
          Serial.println("[HELP] h       toggle touch soft-SPI / hardware SPI3");
          Serial.println("[HELP] s       print status");
          Serial.println("[HELP] g       print the card table (what the desktop configurator edits)");
          Serial.println("[HELP] g clear erase user config -> back to apps.h / icons.h defaults");
          Serial.println("[HELP] w       wifi provisioning status (AP ssid/pass/url + the QR text)");
          Serial.println("[HELP] w clear forget the phone-provisioned wifi, fall back to secrets.h");
          Serial.println("[HELP] w qr    redraw the boot screen (re-encodes the QR, logs its version)");
          Serial.println("[HELP] b <n>   set backlight 0..255");
          Serial.println("[HELP] x       erase stored calibration (then z to reboot)");
          Serial.println("[HELP] z       reboot");
        } else if (cmd == 't') {
          g_touchDebug = !g_touchDebug;
          Serial.printf("[DBG] touch debug %s\n", g_touchDebug ? "ON" : "OFF");
        } else if (cmd == 'l') {
          int sp = arg.indexOf(' ');
          if (arg.startsWith("raw ")) {
            // 'l raw <rx> <ry> [按住毫秒]' —— 直接喂一对裸 ADC 值。
            // 标定向导读的是 getTouchRaw()，光合成屏幕坐标喂不进去，
            // 所以要有这条才能在没有手指的情况下把整个标定向导跑完。
            String r = arg.substring(4);
            r.trim();
            int a = r.indexOf(' ');
            int rx = r.substring(0, a).toInt();
            String r2 = r.substring(a + 1);
            r2.trim();
            int b = r2.indexOf(' ');
            int ry, hold = 300;
            if (b >= 0) {
              ry   = r2.substring(0, b).toInt();
              hold = r2.substring(b + 1).toInt();
              if (hold < 1) hold = 1;
            } else {
              ry = r2.toInt();
            }
            g_synthRawX = rx; g_synthRawY = ry;
            g_synthRawUntil = millis() + (uint32_t)hold;
            g_synthRawOn = true;
            lgfx::touch_point_t sp2;
            sp2.x = (int16_t)rx; sp2.y = (int16_t)ry;
            tft.convertRawXY(&sp2, 1);
            Serial.printf("[SYNTHRAW] raw(%d,%d) hold=%dms -> screen(%d,%d)\n",
                          rx, ry, hold, (int)sp2.x, (int)sp2.y);
          } else if (sp >= 0) {
            // 'l <x> <y> [按住毫秒]' = 在屏幕坐标 (x,y) 合成一次点击，走完整的点击链路。
            // 第三个参数用来测长按（>=5000 会进标定向导）。
            int sx = arg.substring(0, sp).toInt();
            String rest = arg.substring(sp + 1);
            rest.trim();
            int sp2 = rest.indexOf(' ');
            int sy, hold = 250;
            if (sp2 >= 0) {
              sy   = rest.substring(0, sp2).toInt();
              hold = rest.substring(sp2 + 1).toInt();
              if (hold < 1) hold = 1;
            } else {
              sy = rest.toInt();
            }
            synthTap(sx, sy, (uint32_t)hold);
          } else {
            int id = arg.toInt();
            for (int i = 0; i < APP_COUNT; i++) {
              if (g_app[i].id == id) { launchApp(i); break; }
            }
          }
        } else if (cmd == 'd') {                 // 读屏幕控制器 ID (RDDID 0x04)
          tft.probeTftId();
        } else if (cmd == 'o') {                 // 显示方向 / 反色（改完存 NVS）
          // 'o'          = 打印当前值
          // 'o p <0-7>'  = 面板 offset_rotation（bit2=上下翻转；ILI9341 版 CYD 用 2）
          // 'o t <0-7>'  = 触摸 offset_rotation（只影响触摸，不动显示）
          // 'o r <0-3>'  = 逻辑 rotation
          // 'o i'        = 反色开/关
          // 'o d'        = 全部恢复成编译期默认值
          // 前四个都会立刻生效、把画面重画一遍方便看效果，但**都不写 flash** ——
          // 重启就回到编译期配置。试出对的值之后要改 board.h / platformio.ini 再烧一次。
          char which = arg.length() ? tolower(arg[0]) : 0;
          bool changed = false;

          if (which == 'p') {
            g_dispOff = (uint8_t)(arg.substring(1).toInt() & 7);
            changed = true;
          } else if (which == 'r' && arg.length() > 1) {
            g_dispRot = (uint8_t)(arg.substring(1).toInt() & 3);
            changed = true;
          } else if (which == 'i') {
            g_dispInv = !g_dispInv;
            changed = true;
          } else if (which == 'd') {
            g_dispOff = PANEL_OFFSET_ROTATION;
            g_dispRot = SCREEN_ROTATION;
            g_dispInv = (TFT_INVERT != 0);
            changed = true;
          } else if (which == 't') {
            uint8_t v = (uint8_t)(arg.substring(1).toInt() & 7);
            tft.setTouchOffsetRotation(v);
            Serial.printf("[DBG] touch offset_rotation = %u\n", (unsigned)v);
          } else if (arg.length() && which != 'p') {
            Serial.println("[DBG] 用法: o | o p <0-7> | o t <0-7> | o r <0-3> | o i | o d");
          }

          if (changed) {
            applyDisplay();
            dispSaveNvs();
            if (g_bootScreen) drawSelfTest(); else drawGrid();
          }
          Serial.printf("[DBG] disp off=%u rot=%u inv=%u  touch_off=%u  %dx%d\n",
                        (unsigned)g_dispOff, (unsigned)g_dispRot, (unsigned)g_dispInv,
                        (unsigned)tft.touchOffsetRotation(), g_sw, g_sh);
        } else if (cmd == 'r') {
          g_touchRawDebug = !g_touchRawDebug;
          Serial.printf("[DBG] touch RAW debug %s\n", g_touchRawDebug ? "ON" : "OFF");
        } else if (cmd == 'c') {                 // cal -> 触摸标定向导
          calStart();
        } else if (cmd == 'i') {                 // 开关触摸 IRQ 引脚
          bool on = !tft.touchIrqEnabled();
          tft.setTouchIrq(on);
          Serial.printf("[DBG] touch IRQ %s\n",
                        on ? "ON" : "OFF (always poll SPI)");
        } else if (cmd == 'h') {                 // 触摸软件 SPI <-> 硬件 SPI3
          bool soft = !tft.touchSoftSpi();
          tft.setTouchSoftSpi(soft);
          Serial.printf("[DBG] touch SPI = %s\n",
                        soft ? "software SPI (official CYD)" : "hardware SPI3");
        } else if (cmd == 'p') {                 // 原始 SPI 采样（绕过 LovyanGFX 过滤）
          if (arg.length()) {
            tft.probeTouchCs(arg.toInt());       // 'p <引脚>' = CS 对照实验
          } else {
            g_touchProbe = !g_touchProbe;
            Serial.printf("[DBG] touch SPI probe %s\n", g_touchProbe ? "ON" : "OFF");
            if (g_touchProbe) {
              Serial.println("[DBG] 别碰屏幕抓几组，再按住屏幕抓几组，对比 x/y 有没有变化");
              Serial.println("[DBG] 两组数字完全一样 -> SPI 链路没通 / 控制器没响应");
              Serial.println("[DBG] 数字变了但卡片点不中 -> 链路是好的，用 'c' 重新标定");
            }
          }
        } else if (cmd == 'x') {                 // 忘掉 NVS 里存的标定
          Preferences p;
          if (p.begin(NVS_CAL_NS, false)) { p.clear(); p.end(); }
          Serial.println("[DBG] NVS calibration cleared (send 'z' to reboot)");
        } else if (cmd == 'z') {
          Serial.println("[DBG] rebooting");
          delay(50);
          ESP.restart();
        } else if (cmd == 'b') {
          int v = constrain(arg.toInt(), 0, 255);
          g_bright = (uint8_t)v;
          tft.setBrightness(v);
          Serial.printf("[DBG] brightness %d\n", v);
        } else if (cmd == 'g') {                 // 卡片表（桌面配置器改的就是这张表）
          if (arg == "clear" || arg == "reset") {
            appStoreReset();
            drawGrid();
            Serial.println("[CFG] cleared -> factory defaults");
          } else {
            Serial.printf("[CFG] spiffs=%d  custom=%d  port=%d\n",
                          (int)g_fsOk, (int)appStoreHasCustom(), CFG_PORT);
            for (int i = 0; i < APP_COUNT; i++)
              Serial.printf("[CFG]   %d  id=%-3u rgb=%06X  iconFs=%d  \"%s\"\n",
                            i, (unsigned)g_app[i].id, (unsigned)g_app[i].rgb,
                            (int)g_appIconFs[i], g_app[i].label);
          }
        } else if (cmd == 'w') {                 // 扫码配网状态
          if (arg == "clear" || arg == "reset") {
            // 忘掉扫码存过的账号，退回 secrets.h —— 配网搞砸了可以用它兜底
            provClearCred();
            Serial.println("[PROV] NVS 账号已清除，重启后回退 secrets.h（发 'z' 重启）");
          } else if (arg == "qr") {
            drawSelfTest();                      // 重画一遍，顺便把二维码的过程打到串口
          } else {
            provPrintState();
          }
        } else if (cmd == 's') {
          uint16_t cx0, cx1, cy0, cy1;
          tft.getTouchCal(cx0, cx1, cy0, cy1);
          Serial.printf("[DBG] wifi=%d ip=%s link=%d heap=%u\n",
                        (int)WiFi.status(), WiFi.localIP().toString().c_str(),
                        g_link, (unsigned)ESP.getFreeHeap());
          Serial.printf("[DBG] touch cal x %u..%u  y %u..%u  irq=%d  spi=%s  cal=%d\n",
                        cx0, cx1, cy0, cy1, tft.touchIrqEnabled() ? 1 : 0,
                        tft.touchSoftSpi() ? "soft" : "hw3", (int)g_cal);
          // 网格几何 + 每张卡的中心：合成点击（`l <x> <y>`）要用这些坐标，
          // 改完布局先看这几行，别靠肉眼数卡片。
          {
            int cols = (g_sw >= g_sh) ? 3 : 2;
            int rows = (APP_COUNT + cols - 1) / cols;
            Rect r0 = tileRect(0);
            Serial.printf("[DBG] screen %dx%d rot=%u off=%u inv=%d  grid %dx%d  tile0 (%d,%d) %dx%d\n",
                          g_sw, g_sh, (unsigned)tft.getRotation(),
                          (unsigned)g_dispOff, (int)g_dispInv,
                          cols, rows, r0.x, r0.y, r0.w, r0.h);
            for (int i = 0; i < APP_COUNT; i++) {
              Rect r = tileRect(i);
              Serial.printf("[DBG]   tile %d %-9s center (%d,%d)\n",
                            i, g_app[i].label, r.x + r.w / 2, r.y + r.h / 2);
            }
          }
        }
      }
      sbuf = "";
    } else if (sbuf.length() < 40) {
      sbuf += c;
    }
  }
#endif
}

// ---------------------------------------------------------------------------
// WiFi
// ---------------------------------------------------------------------------
#ifdef USE_STATIC_IP
static IPAddress parseIP(const char* s, const IPAddress& fallback) {
  int a = 0, b = 0, c = 0, d = 0;
  if (s && sscanf(s, "%d.%d.%d.%d", &a, &b, &c, &d) == 4)
    return IPAddress((uint8_t)a, (uint8_t)b, (uint8_t)c, (uint8_t)d);
  return fallback;
}
#endif

static void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  uint32_t now = millis();
  if ((int32_t)(now - g_wifiRetryAt) < 0) return;
  g_wifiRetryAt = now + WIFI_RETRY_MS;
  g_wifiTries++;

  Serial.printf("[WiFi] retry #%d (status=%d)\n", g_wifiTries, (int)WiFi.status());
  if (WIFI_MAX_TRY > 0 && g_wifiTries > WIFI_MAX_TRY) {
    Serial.println("[WiFi] giving up, restarting");
    ESP.restart();
  }
  String s, p;
  provEffectiveCred(s, p);
  WiFi.disconnect();
  WiFi.begin(s.c_str(), p.c_str());
  drawStatusBar();
}

static void onWifiUp() {
  static bool announced = false;
  if (WiFi.status() != WL_CONNECTED) { announced = false; return; }
  if (announced) return;
  announced = true;
  g_wifiTries = 0;
  Serial.printf("[WiFi] connected: %s\n", WiFi.localIP().toString().c_str());

  if (MDNS.begin(DEVICE_NAME)) {
    MDNS.addService("esp32launcher", "tcp", TCP_PORT);
    Serial.printf("[mDNS] %s.local\n", DEVICE_NAME);
  }
  g_server.end();
  g_server.begin();
  g_server.setNoDelay(true);
  g_cfgServer.end();                 // 配置端口（8269）跟着 WiFi 一起上线
  g_cfgServer.begin();
  g_cfgServer.setNoDelay(true);
  g_udp.begin(UDP_PORT);
  otaBegin();                        // WiFi 一上来就把 OTA 挂上
  postMsg("WiFi %s", WiFi.localIP().toString().c_str());
  drawStatusBar();
}

// ---------------------------------------------------------------------------
// setup / loop
// ---------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("=== ESP32 App Launcher ===");
  Serial.printf("profile=%d rotation=%d\n", BOARD_PROFILE, SCREEN_ROTATION);

  tft.init();
  tft.setBrightness(IDLE_BRIGHT);
  dispLoadNvs();         // 屏幕方向/反色：优先用 NVS 里存过的（见文件上方那段说明）
  applyDisplay();        // 写进面板、顺便把 g_sw/g_sh 刷成实际尺寸
  calLoadNvs();          // 有存过的标定就用它，否则用 board.h 里的编译期默认值

  // 卡片表的运行时覆盖层：图标在 SPIFFS、文字/颜色在 NVS（见 appstore.h）。
  // SPIFFS 挂不上也不能死机 —— 直接退回 apps.h / icons.h 的出厂值，屏上照常能用。
  g_fsOk = SPIFFS.begin(true);
  appStoreInit();
  bool custom = appStoreLoad();
  Serial.printf("[CFG] spiffs %s  custom %s  port %d  iconbytes %d\n",
                g_fsOk ? "ok" : "FAIL", custom ? "yes" : "no", CFG_PORT, ICON_BYTES);
  for (int i = 0; i < APP_COUNT; i++)
    if (g_appIconFs[i]) Serial.printf("[CFG]   icon %d <- spiffs\n", i);

  // 自检：把标定向导的两个靶心换算到"面板原始方向"。
  // 正确性由随后的向导往返测试验证（串口 `c` 然后用 `l raw ...` 喂已知值，
  // 看能不能精确解回同一组标定），这里只把值打出来备查。
  {
    int p1x, p1y, p2x, p2y;
    tft.screenToPanelSpace(26, 26, p1x, p1y);
    tft.screenToPanelSpace(g_sw - 27, g_sh - 27, p2x, p2y);
    Serial.printf("[CAL] selftest rot=%u off=%u panel targets (%d,%d) (%d,%d)\n",
                  (unsigned)tft.getRotation(), (unsigned)tft.panelOffsetRotation(),
                  p1x, p1y, p2x, p2y);
  }

  // 扫码配网：热点和配网网页必须在画自检画面之前拉起来 ——
  // drawSelfTest() 要用 g_provOn 和 provQrText() 才能把二维码画出来。
  provBegin();
  drawSelfTest();

  WiFi.setSleep(false);
#ifdef USE_STATIC_IP
  {
    IPAddress ip   = parseIP(STATIC_IP,   IPAddress(0, 0, 0, 0));
    IPAddress gw   = parseIP(STATIC_GW,   IPAddress(0, 0, 0, 0));
    IPAddress mask = parseIP(STATIC_MASK, IPAddress(255, 255, 255, 0));
    IPAddress dns  = parseIP(STATIC_DNS,  gw);
    if (WiFi.config(ip, gw, mask, dns))
      Serial.printf("[WiFi] static ip %s\n", ip.toString().c_str());
    else
      Serial.println("[WiFi] static ip config FAILED, falling back to DHCP");
  }
#endif
  {
    // 有扫码存过的就用扫码的，没有就用 secrets.h 的 —— 老用户升级上来行为不变
    String s, p;
    provEffectiveCred(s, p);
    Serial.printf("[WiFi] begin '%s' (%s)\n", s.c_str(),
                  g_provHaveCred ? "NVS" : "secrets.h");
    WiFi.begin(s.c_str(), p.c_str());
  }

  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 12000) {
    delay(200);
    Serial.print('.');
  }
  Serial.println();

  onWifiUp();
  // 预热配网页的网络列表（要扫 9 秒）。放在这里：二维码早就画好了，倒计时也还没开始，
  // 用户扫码的时候页面是秒开的。详见 prov.h 里 provWarmScan 的注释。
  provWarmScan();
  if (!g_bootScreen) drawGrid();     // 自检画面期间先别盖掉它
  g_bootAt = millis();               // 倒计时从 setup 结束开始算（WiFi 那 12s + 扫描那 9s 不算进去）
}

void loop() {
  ArduinoOTA.handle();   // 必须每圈都调：OTA 的接收就是在它里面完成的

  // 升级期间把其它活儿全停掉 —— 别去抢屏幕，也别让 TCP / 触摸干扰这次传输
  if (g_otaActive) { delay(1); return; }

  // 配网网页 / 强制门户：自检画面期间也得转，不然手机连上热点后
  // 一直卡在"正在登录"打不开页面（用户第一反应就是扫码的那 20 秒内去连）。
  provPoll();

  // 开机自检画面：等够 SELFTEST_MS 就切到正常界面。
  // 这期间点屏幕 = 换朝向、按住 = 反色（pollDispSetup），每次操作都会推后倒计时。
  if (g_bootScreen) {
    pollDispSetup();
    if ((int32_t)(millis() - (g_bootAt + SELFTEST_MS)) >= 0) {
      g_bootScreen = false;
      Serial.println("[UI] boot selftest -> grid");
      drawGrid();
    }
    pollSerial();                    // 串口仍然要能用，否则这段时间没法调试
    pollCfg();                       // 配置端口也早点开，不然刚开机连不上
    delay(5);
    return;
  }

  ensureWifi();
  onWifiUp();
  pollUdp();
  pollClient();
  pollCfg();
  pollCal();
  pollTouch();
  pollTimeouts();
  pollSerial();
  // 'p' 打开的原始 SPI 采样：2.5 次/秒，人来得及"松手抓几组、按住抓几组"
  if (g_touchProbe) {
    static uint32_t last = 0;
    if (millis() - last >= 400) {
      last = millis();
      int ok = tft.probeTouchRaw();
      Serial.printf("[PROBE] --- %d/7 in range ---\n", ok);
    }
  }
  delay(5);
}
