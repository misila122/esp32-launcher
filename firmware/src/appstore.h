// ===========================================================================
//  appstore.h —— 卡片表的"运行时覆盖层"
//
//  为什么要这一层：
//    卡片的名字/颜色/图标原来是写死在 apps.h + icons.h 里的，改一个字母就得
//    重编译 2 分钟再 OTA 推一次。桌面配置器要的是"点确定 2 秒生效"，所以把
//    用户改过的东西挪到板上可写的存储里：
//
//      文字 / 颜色 / id  ->  NVS（一个 24 字节 x 6 的 blob，很小）
//      64x64 图标        ->  SPIFFS（每个 8192 字节，6 个共 48KB）
//
//    开机时先铺编译期的出厂值，再用 NVS + SPIFFS 盖上。
//    所以 "CLEAR"（或整片 erase_flash）之后会自动退回 apps.h / icons.h 的出厂值，
//    永远不会出现"配置坏了屏上一片空白"。
// ===========================================================================
#pragma once
#include <Preferences.h>
#include <SPIFFS.h>
#include "apps.h"
#include "icons.h"

#define APP_NVS_NS     "apps"
#define APP_LABEL_MAX  16                 // 显示名最多 16 个字符（不含结尾 0）
#define ICON_BYTES     (ICON_W * ICON_H * 2)

// 存进 NVS 的一格。24 字节，6 格一共 144 字节。
struct StoredSlot {
  uint8_t  used;                          // 0 = 这一格没被用户改过，用编译期默认
  uint8_t  id;                            // 发给 PC 的编号
  uint16_t pad;
  uint32_t rgb;                           // 主题色 0xRRGGBB
  char     label[APP_LABEL_MAX + 1];
};

static AppDef   g_app[APP_COUNT];                 // 运行时卡片表
static char     g_appLabel[APP_COUNT][APP_LABEL_MAX + 1];
static bool     g_appIconFs[APP_COUNT];           // 该格图标是不是来自 SPIFFS
static uint16_t g_appIconBuf[ICON_W * ICON_H];    // 读 SPIFFS 图标用的复用缓冲
static bool     g_fsOk = false;                   // SPIFFS 挂上了没

// 只保留可见 ASCII：Font2 没有中文字库，塞进去只会画出一堆方块。
static void appSanitize(char* s, size_t n) {
  size_t o = 0;
  for (size_t i = 0; s[i] && o < n - 1; i++) {
    unsigned char c = (unsigned char)s[i];
    if (c < 0x20 || c > 0x7E) continue;
    s[o++] = (char)c;
  }
  s[o] = 0;
}

static void appStoreInit() {
  for (int i = 0; i < APP_COUNT; i++) {
    g_app[i] = APPS[i];
    strncpy(g_appLabel[i], APPS[i].label, APP_LABEL_MAX);
    g_appLabel[i][APP_LABEL_MAX] = 0;
    g_app[i].label = g_appLabel[i];
    g_appIconFs[i] = false;
  }
}

static void appIconPath(int i, char* out, size_t n) {
  snprintf(out, n, "/icon%d.bin", i);
}

static bool appIconExists(int i) {
  if (!g_fsOk) return false;
  char p[24];
  appIconPath(i, p, sizeof(p));
  File f = SPIFFS.open(p, "r");
  if (!f) return false;
  bool ok = ((size_t)f.size() == ICON_BYTES);
  f.close();
  return ok;
}

// drawTile 每画一格调一次。优先用 SPIFFS 里用户推上来的图标，
// 没有就退回 icons.h 里编译进去的那个。
static const uint16_t* appIcon(int i) {
  if (g_appIconFs[i]) {
    char p[24];
    appIconPath(i, p, sizeof(p));
    File f = SPIFFS.open(p, "r");
    if (f && (size_t)f.size() == ICON_BYTES) {
      size_t got = f.read((uint8_t*)g_appIconBuf, ICON_BYTES);
      f.close();
      if (got == ICON_BYTES) return g_appIconBuf;
    } else if (f) {
      f.close();
    }
    g_appIconFs[i] = false;               // 文件坏了/没了，退回编译期图标
  }
  return ICONS[i];
}

static bool appStoreLoad() {
  Preferences p;
  if (!p.begin(APP_NVS_NS, true)) return false;
  bool used = p.getBool("used", false);
  StoredSlot buf[APP_COUNT];
  size_t n = used ? p.getBytes("table", buf, sizeof(buf)) : 0;
  p.end();

  bool any = false;
  if (n == sizeof(buf)) {
    for (int i = 0; i < APP_COUNT; i++) {
      if (!buf[i].used) continue;
      g_app[i].id  = buf[i].id;
      g_app[i].rgb = buf[i].rgb;
      memcpy(g_appLabel[i], buf[i].label, sizeof(buf[i].label));
      g_appLabel[i][APP_LABEL_MAX] = 0;
      appSanitize(g_appLabel[i], sizeof(g_appLabel[i]));
      g_app[i].label = g_appLabel[i];
      any = true;
    }
  }
  for (int i = 0; i < APP_COUNT; i++) g_appIconFs[i] = appIconExists(i);
  return any;
}

static bool appStoreSave() {
  StoredSlot buf[APP_COUNT];
  memset(buf, 0, sizeof(buf));
  for (int i = 0; i < APP_COUNT; i++) {
    buf[i].used = 1;
    buf[i].id   = g_app[i].id;
    buf[i].rgb  = g_app[i].rgb;
    strncpy(buf[i].label, g_app[i].label, APP_LABEL_MAX);
  }
  Preferences p;
  if (!p.begin(APP_NVS_NS, false)) return false;
  size_t n = p.putBytes("table", buf, sizeof(buf));
  p.putBool("used", true);
  p.end();
  return n == sizeof(buf);
}

// 抹掉用户改动，回到 apps.h / icons.h 的出厂值
static bool appStoreReset() {
  Preferences p;
  bool ok = p.begin(APP_NVS_NS, false);
  if (ok) {
    p.clear();
    p.end();
  }
  if (g_fsOk) {
    for (int i = 0; i < APP_COUNT; i++) {
      char path[24];
      appIconPath(i, path, sizeof(path));
      if (SPIFFS.exists(path)) SPIFFS.remove(path);
    }
  }
  appStoreInit();
  return ok;
}

static bool appStoreHasCustom() {
  for (int i = 0; i < APP_COUNT; i++) if (g_appIconFs[i]) return true;
  Preferences p;
  if (!p.begin(APP_NVS_NS, true)) return false;
  bool used = p.getBool("used", false);
  p.end();
  return used;
}
