// ===========================================================================
//  apps.h —— 屏幕上这 6 个格子分别对应哪个程序（出厂默认值）
//
//  这个文件是 ESP32 启动器配置器生成的，别手改 —— 下次点「烧进固件」会覆盖。
//  想改就开配置器改，或者直接改板子上的（点「应用」，走 NVS + SPIFFS）。
//
//  id 必须和 PC 端 pc/apps.json 里的 id 一一对应！
//  改这里 = 改屏幕；改 apps.json = 改电脑上真正启动什么。两边 id 对上就行。
//
//  板子运行时用 NVS + SPIFFS 里的配置盖在这个默认值上面，
//  只有在「恢复出厂」或整片擦除之后才会回到这里。
// ===========================================================================
#pragma once
#include <stdint.h>

enum GlyphKind : uint8_t {
  GLYPH_TERMINAL = 0,
  GLYPH_BOLT,
  GLYPH_TV,
  GLYPH_CHART,
  GLYPH_GAMEPAD,
  GLYPH_CHAT,
};

struct AppDef {
  uint8_t     id;      // 发给 PC 的编号
  const char* label;   // 屏幕上显示的名字（只能是 ASCII，中文要额外做字库）
  uint8_t     glyph;   // 图标形状（没有自定义图标时用它兜底）
  uint32_t    rgb;     // 主题色 0xRRGGBB
};

static const AppDef APPS[] = {
  { 1, "Notepad", GLYPH_TERMINAL, 0x4C8BF5 },
  { 2, "Calc", GLYPH_CHART, 0x7B61FF },
  { 3, "Terminal", GLYPH_TERMINAL, 0xF0B90B },
  { 4, "Paint", GLYPH_GAMEPAD, 0xFB7299 },
  { 5, "Explorer", GLYPH_TV, 0x00C2A8 },
  { 6, "Chat", GLYPH_CHAT, 0x07C160 },
};

static const int APP_COUNT = (int)(sizeof(APPS) / sizeof(APPS[0]));
