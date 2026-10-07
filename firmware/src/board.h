// ===========================================================================
//  board.h —— 屏幕 + 触摸的硬件抽象层
//
//  为什么单独一个文件：不同 ESP32 板子的屏幕驱动/引脚/触摸芯片完全不同，
//  但上层 UI 代码不该关心这些。所以这里用 BOARD_PROFILE 做编译期切换，
//  换板子只改 platformio.ini 里的一个数字。
//
//  当前支持的 profile：
//    1 = ESP32-2432S028R "Cheap Yellow Display" (2.8" ILI9341 320x240 + XPT2046 电阻触摸)
//    2 = 通用 2.4"/2.8" SPI 屏 (ILI9341) + XPT2046，屏和触摸共用一条 SPI 总线
//    3 = ST7789 240x320 + FT6236 电容触摸 (I2C)
//    4 = 1.14"/1.3" ST7789 135x240（无触摸，只能用串口/网络触发）
//    5 = 3.5" ST7796 320x480 + XPT2046
//
//  要加自己的板子：照抄一个 profile，改引脚和驱动类名即可。
// ===========================================================================
#pragma once

#define LGFX_USE_V1
#include <LovyanGFX.hpp>
#include <array>

#ifndef BOARD_PROFILE
#define BOARD_PROFILE 1
#endif

#ifndef SCREEN_ROTATION
#define SCREEN_ROTATION 1
#endif

#define PROFILE_CYD             1
#define PROFILE_ILI9341_XPT2046 2
#define PROFILE_ST7789_FT6236   3
#define PROFILE_ST7789_114      4
#define PROFILE_ST7796_XPT2046  5

// ---------------------------------------------------------------------------
// 各 profile 的引脚 / 面板参数
// ---------------------------------------------------------------------------

// ---- 1) ESP32-2432S028R (CYD)：屏幕和触摸各占一条 SPI 总线 ----------------
#if BOARD_PROFILE == PROFILE_CYD
  // 这块是 USB-C 双接口批次（2432S028Rv3），屏幕控制器为 ST7789。
  // 将它当 ILI9341 初始化会出现固定宽度的彩色噪点和画面错位。
  #define PANEL_DRIVER_ST7789 1
  #define PANEL_W 240
  #define PANEL_H 320
  // ST7789 版面板不需要显示方向偏移，但其颜色需要反相。
  #define PANEL_OFFSET_ROTATION 0
  #define TFT_INVERT 1

  #define PIN_TFT_SCLK  14
  #define PIN_TFT_MOSI  13
  #define PIN_TFT_MISO  12
  #define PIN_TFT_CS    15
  #define PIN_TFT_DC     2
  #define PIN_TFT_RST   -1
  #define PIN_TFT_BL    21

  #define HAS_TOUCH      1
  #define TOUCH_XPT2046  1
  #define PIN_TOUCH_SCLK 25
  #define PIN_TOUCH_MOSI 32
  #define PIN_TOUCH_MISO 39
  #define PIN_TOUCH_CS   33
  #define PIN_TOUCH_IRQ  36
  // 默认不使用 IRQ 引脚 —— LovyanGFX 自带的 CYD 识别器
  // (_detector_Sunton_ESP32_2432S028_9341_t) 就是 pin_int = -1。
  // 原因：用了 IRQ 后 Touch_XPT2046::getTouchRaw() 开头就是
  //   if (_cfg.pin_int >= 0 && lgfx::gpio_in(_cfg.pin_int)) return 0;
  // IRQ 一旦接得不对/极性不符，触摸会彻底失灵且没有任何报错。
  // 串口发 `i` 可以临时打开它做 A/B 对比。
  #define TOUCH_USE_IRQ  0
  // 起始标定值，抄自 LovyanGFX 的 CYD 识别器。注意 y 是倒着的
  // (y_min > y_max) —— 这是 XPT2046 焊在这块屏上的真实方向，不是笔误。
  // 这些只是起点：跑一次串口 `cal` 会用实测值覆盖并存进 NVS。
  #define TOUCH_CAL_XMIN 300
  #define TOUCH_CAL_XMAX 3900
  #define TOUCH_CAL_YMIN 3700
  #define TOUCH_CAL_YMAX 200
  // ST7789 版的触摸与屏幕坐标相差 180°。
  #define TOUCH_OFFSET_ROTATION 2

// ---- 2) 通用 ILI9341 + XPT2046（共用一条 SPI 总线） ----------------------
#elif BOARD_PROFILE == PROFILE_ILI9341_XPT2046
  #define PANEL_DRIVER_ILI9341 1
  #define PANEL_W 240
  #define PANEL_H 320
  #define TFT_INVERT 1

  #define PIN_TFT_SCLK  18
  #define PIN_TFT_MOSI  23
  #define PIN_TFT_MISO  19
  #define PIN_TFT_CS    15
  #define PIN_TFT_DC     2
  #define PIN_TFT_RST    4
  #define PIN_TFT_BL    32

  #define HAS_TOUCH      1
  #define TOUCH_XPT2046  1
  #define TOUCH_SHARED_BUS 1        // 触摸挂在屏幕那条 SPI 上
  #define PIN_TOUCH_CS   21
  #define PIN_TOUCH_IRQ  22
  #define TOUCH_USE_IRQ  0            // 同上：先不用 IRQ，稳妥
  #define TOUCH_CAL_XMIN 300
  #define TOUCH_CAL_XMAX 3900
  #define TOUCH_CAL_YMIN 3700
  #define TOUCH_CAL_YMAX 200

// ---- 3) ST7789 240x320 + FT6236 电容触摸（I2C） --------------------------
#elif BOARD_PROFILE == PROFILE_ST7789_FT6236
  #define PANEL_DRIVER_ST7789 1
  #define PANEL_W 240
  #define PANEL_H 320
  #define TFT_INVERT 1

  #define PIN_TFT_SCLK  18
  #define PIN_TFT_MOSI  23
  #define PIN_TFT_MISO  -1
  #define PIN_TFT_CS    15
  #define PIN_TFT_DC     2
  #define PIN_TFT_RST    4
  #define PIN_TFT_BL    32

  #define HAS_TOUCH      1
  #define TOUCH_FT6236   1
  #define PIN_TOUCH_SDA  21
  #define PIN_TOUCH_SCL  22
  #define TOUCH_I2C_ADDR 0x38

// ---- 4) 1.14" ST7789 135x240，无触摸 -------------------------------------
#elif BOARD_PROFILE == PROFILE_ST7789_114
  #define PANEL_DRIVER_ST7789 1
  #define PANEL_W 135
  #define PANEL_H 240
  #define TFT_INVERT 1
  #define PANEL_OFFSET_X 52
  #define PANEL_OFFSET_Y 40

  #define PIN_TFT_SCLK  18
  #define PIN_TFT_MOSI  19
  #define PIN_TFT_MISO  -1
  #define PIN_TFT_CS     5
  #define PIN_TFT_DC    16
  #define PIN_TFT_RST   23
  #define PIN_TFT_BL     4
  #define HAS_TOUCH      0

// ---- 5) 3.5" ST7796 320x480 + XPT2046 ------------------------------------
#elif BOARD_PROFILE == PROFILE_ST7796_XPT2046
  #define PANEL_DRIVER_ST7796 1
  #define PANEL_W 320
  #define PANEL_H 480
  #define TFT_INVERT 1

  #define PIN_TFT_SCLK  14
  #define PIN_TFT_MOSI  13
  #define PIN_TFT_MISO  12
  #define PIN_TFT_CS    15
  #define PIN_TFT_DC     2
  #define PIN_TFT_RST   -1
  #define PIN_TFT_BL    27

  #define HAS_TOUCH      1
  #define TOUCH_XPT2046  1
  #define PIN_TOUCH_SCLK 25
  #define PIN_TOUCH_MOSI 32
  #define PIN_TOUCH_MISO 39
  #define PIN_TOUCH_CS   33
  #define PIN_TOUCH_IRQ  36
  #define TOUCH_USE_IRQ  0
  #define TOUCH_CAL_XMIN 300
  #define TOUCH_CAL_XMAX 3900
  #define TOUCH_CAL_YMIN 3700
  #define TOUCH_CAL_YMAX 200

#else
  #error "BOARD_PROFILE 无效：只能是 1..5"
#endif

#if !defined(HAS_TOUCH)
  #define HAS_TOUCH 0
#endif

// 只有 CYD 需要非 0 的 offset_rotation；其余 profile 用标准起始角。
#if !defined(PANEL_OFFSET_ROTATION)
  #define PANEL_OFFSET_ROTATION 0
#endif
#if !defined(TOUCH_OFFSET_ROTATION)
  #define TOUCH_OFFSET_ROTATION 0
#endif

// ---------------------------------------------------------------------------
// LovyanGFX 设备类：把上面的宏翻译成驱动配置
// ---------------------------------------------------------------------------
class LGFX : public lgfx::LGFX_Device {
#if defined(PANEL_DRIVER_ILI9341)
  lgfx::Panel_ILI9341 _panel;
#elif defined(PANEL_DRIVER_ST7796)
  lgfx::Panel_ST7796  _panel;
#else
  lgfx::Panel_ST7789  _panel;
#endif
  lgfx::Bus_SPI _bus_tft;
  lgfx::Light_PWM _light;

// 注意：LovyanGFX v1 的 ITouch 自己管 SPI/I2C 总线，引脚直接写在
// touch 的 config 里，没有 setBus()。所以这里不给触摸单独的 Bus 成员。
#if defined(TOUCH_XPT2046)
  lgfx::Touch_XPT2046 _touch;
#elif defined(TOUCH_FT6236)
  lgfx::Touch_FT5x06 _touch;
#endif

public:
  LGFX() {
    // ---------------- 屏幕 SPI ----------------
    {
      auto cfg = _bus_tft.config();
      cfg.spi_host    = SPI2_HOST;
      cfg.spi_mode    = 0;
      cfg.freq_write  = 40000000;
      cfg.freq_read   = 16000000;
      cfg.spi_3wire   = (PIN_TFT_MISO < 0);
      cfg.use_lock    = true;
      cfg.dma_channel = SPI_DMA_CH_AUTO;
      cfg.pin_sclk    = PIN_TFT_SCLK;
      cfg.pin_mosi    = PIN_TFT_MOSI;
      cfg.pin_miso    = PIN_TFT_MISO;
      cfg.pin_dc      = PIN_TFT_DC;
      _bus_tft.config(cfg);
    }

    // ---------------- 面板 ----------------
    {
      auto cfg = _panel.config();
      cfg.pin_cs   = PIN_TFT_CS;
      cfg.pin_rst  = PIN_TFT_RST;
      cfg.pin_busy = -1;
      cfg.panel_width  = PANEL_W;
      cfg.panel_height = PANEL_H;
#ifdef PANEL_OFFSET_X
      cfg.offset_x = PANEL_OFFSET_X;
      cfg.offset_y = PANEL_OFFSET_Y;
#endif
      cfg.offset_rotation = PANEL_OFFSET_ROTATION;
      cfg.invert = (TFT_INVERT != 0);   // 颜色反了就把 platformio.ini 的 TFT_INVERT 改成 0
      cfg.rgb_order = false;
      cfg.dlen_16bit = false;
      _panel.config(cfg);
      _panel.setBus(&_bus_tft);
    }

    // ---------------- 背光 ----------------
#if PIN_TFT_BL >= 0
    {
      auto cfg = _light.config();
      cfg.pin_bl      = PIN_TFT_BL;
      cfg.invert      = false;
      cfg.freq        = 12000;
      cfg.pwm_channel = 7;
      _light.config(cfg);
      _panel.setLight(&_light);
    }
#endif

    // ---------------- 触摸 ----------------
    // LovyanGFX v1：ITouch::config_t 里直接给总线参数（spi_host / pin_sclk /
    // pin_mosi / pin_miso / pin_cs 或 i2c_* ），没有 setBus()。
#if defined(TOUCH_XPT2046)
    {
      auto tcfg = _touch.config();
      tcfg.pin_int  = TOUCH_USE_IRQ ? PIN_TOUCH_IRQ : -1;
      tcfg.pin_cs   = PIN_TOUCH_CS;
      tcfg.freq     = 1000000;
      tcfg.x_min = TOUCH_CAL_XMIN;
      tcfg.x_max = TOUCH_CAL_XMAX;
      tcfg.y_min = TOUCH_CAL_YMIN;
      tcfg.y_max = TOUCH_CAL_YMAX;
      tcfg.offset_rotation = TOUCH_OFFSET_ROTATION;
#if defined(TOUCH_SHARED_BUS)
      tcfg.bus_shared = true;         // 触摸和屏幕共用一条 SPI
      tcfg.spi_host   = SPI2_HOST;    // 跟 _bus_tft 用同一个 host
      tcfg.pin_sclk   = PIN_TFT_SCLK;
      tcfg.pin_mosi   = PIN_TFT_MOSI;
      tcfg.pin_miso   = PIN_TFT_MISO;
#else
      tcfg.bus_shared = false;        // 触摸独占一条 SPI
      // 官方 CYD 识别器用的是【软件 SPI】(spi_host = -1)，不是硬件 SPI3。
      // 见 LGFX_AutoDetect_ESP32_all.hpp:3131 "-1:use software SPI for XPT2046"。
      // 这里跟随官方已知可用的配置；串口 'h' 可以运行时切到硬件 SPI3 做对比。
      tcfg.spi_host   = -1;
      tcfg.pin_sclk   = PIN_TOUCH_SCLK;
      tcfg.pin_mosi   = PIN_TOUCH_MOSI;
      tcfg.pin_miso   = PIN_TOUCH_MISO;
#endif
      _touch.config(tcfg);
      _panel.setTouch(&_touch);
    }
#elif defined(TOUCH_FT6236)
    {
      auto tcfg = _touch.config();
      tcfg.i2c_port = 0;
      tcfg.pin_sda  = PIN_TOUCH_SDA;
      tcfg.pin_scl  = PIN_TOUCH_SCL;
      tcfg.i2c_addr = TOUCH_I2C_ADDR;
      tcfg.freq     = 400000;
#ifdef PIN_TOUCH_IRQ
      tcfg.pin_int  = PIN_TOUCH_IRQ;
#endif
      tcfg.x_min = 0;   tcfg.x_max = PANEL_W - 1;
      tcfg.y_min = 0;   tcfg.y_max = PANEL_H - 1;
      tcfg.bus_shared = false;
      _touch.config(tcfg);
      _panel.setTouch(&_touch);
    }
#endif

    setPanel(&_panel);
  }

  // =========================================================================
  //  运行时触摸标定
  //
  //  XPT2046 是电阻屏，每块板子的 ADC 端点都不一样，"出厂值"只能算个起点。
  //  下面这几个函数让固件不用重新编译就能改标定，并把它存进 NVS。
  // =========================================================================
#if HAS_TOUCH
  void getTouchCal(uint16_t &xmin, uint16_t &xmax, uint16_t &ymin, uint16_t &ymax) const {
    auto c = _touch.config();
    xmin = c.x_min; xmax = c.x_max; ymin = c.y_min; ymax = c.y_max;
  }

  // 写入新标定值并立刻重建仿射矩阵（不用重启就生效）。
  // touchCalibrate() 是 Panel_Device 的公开方法，它把 x_min/x_max/y_min/y_max
  // 四个角点拟合成一个 3x3 仿射矩阵。
  void setTouchCal(uint16_t xmin, uint16_t xmax, uint16_t ymin, uint16_t ymax) {
    auto c = _touch.config();
    c.x_min = xmin; c.x_max = xmax; c.y_min = ymin; c.y_max = ymax;
    _touch.config(c);
    panel()->touchCalibrate();
  }

  // 裸 ADC 读数 -> "面板原始方向"坐标，用【当前标定值】换算。
  // 注意不能拿 convertRawXY() 代替：那个输出的是【屏幕】坐标，旋转已经算进去了。
  void rawToPanel(float rx, float ry, float &px, float &py) const {
    uint16_t x0, x1, y0, y1;
    getTouchCal(x0, x1, y0, y1);
    float w = (float)panel()->config().panel_width;
    float h = (float)panel()->config().panel_height;
    px = (rx - (float)x0) * (w - 1.0f) / ((float)x1 - (float)x0);
    py = (ry - (float)y0) * (h - 1.0f) / ((float)y1 - (float)y0);
  }

  // 由两个"裸 ADC <-> 面板坐标"对应点反解 x_min/x_max/y_min/y_max。
  // 依据：面板坐标 = (ADC - min) * (面板尺寸-1) / (max - min)，是线性的；
  // 两点定一条直线，再把直线外推到"面板 0"和"面板 max"就得到两个端点。
  void solveTouchCal(float rx1, float px1, float rx2, float px2,
                     float ry1, float py1, float ry2, float py2,
                     uint16_t &xmin, uint16_t &xmax, uint16_t &ymin, uint16_t &ymax) const {
    float w = (float)panel()->config().panel_width;
    float h = (float)panel()->config().panel_height;
    float ax = (px2 - px1) / (rx2 - rx1);
    float x0 = rx1 - px1 / ax;
    xmin = (uint16_t)lroundf(x0);
    xmax = (uint16_t)lroundf(x0 + (w - 1.0f) / ax);
    float ay = (py2 - py1) / (ry2 - ry1);
    float y0 = ry1 - py1 / ay;
    ymin = (uint16_t)lroundf(y0);
    ymax = (uint16_t)lroundf(y0 + (h - 1.0f) / ay);
  }

  bool touchIrqEnabled() const { return _touch.config().pin_int >= 0; }

  // 开关触摸 IRQ 引脚。关掉后每一轮都真的去读一次 SPI，
  // 用来排查"IRQ 线没接好 -> 永远读不到触摸"这类问题。
  void setTouchIrq(bool use) {
    auto c = _touch.config();
    c.pin_int = use ? PIN_TOUCH_IRQ : -1;
    _touch.config(c);
    _touch.init();                 // 重新配置 IRQ 引脚方向
  }

  // 触摸走软件 SPI（spi_host < 0）还是硬件 SPI3。
  // 软件 SPI 是官方 CYD 配置，但它比硬件慢；硬件 SPI 在某些板子上更稳。
  bool touchSoftSpi() const { return _touch.config().spi_host < 0; }
  void setTouchSoftSpi(bool soft) {
    if (touchSoftSpi() == soft) return;
    auto c = _touch.config();
    c.spi_host = soft ? -1 : SPI3_HOST;
    _touch.config(c);
    _touch.init();                 // 换 host 必须重新初始化总线
  }

  // 对照组实验：用指定的 CS 引脚做一次同样的 SPI 读，打印指纹。
  // 拿真 CS(33) 和假 CS(某个没接东西的脚) 各读一次：
  //   两次读数不同 -> 芯片真的在被选中并回数据 -> 触摸芯片是活的
  //   两次读数一模一样 -> 那些数据不是这颗芯片给的（比如 MISO 悬空被上拉）
  void probeTouchCs(int cs) const {
    auto *t = touch();
    if (!t) { Serial.println("[CSTEST] no touch device"); return; }
    const auto c = t->config();
    if (cs > -1) pinMode(cs, OUTPUT);

    for (int rep = 0; rep < 3; ++rep) {
      uint8_t data[57];
      memset(data, 0, 8);
      data[ 0] = 0x91;  data[ 2] = 0xB1;  data[ 4] = 0xD1;  data[ 6] = 0xC1;
      data[56] = 0x80;
      memcpy(&data[ 8], data,  8);
      memcpy(&data[16], data, 16);
      memcpy(&data[32], data, 24);

      lgfx::spi::beginTransaction(c.spi_host, c.freq, 0);
      if (cs > -1) lgfx::gpio_lo(cs);
      lgfx::spi::readBytes(c.spi_host, data, 57);
      if (cs > -1) lgfx::gpio_hi(cs);
      lgfx::spi::endTransaction(c.spi_host);

      int x = (data[13] << 8 | data[14]) >> 3;
      int y = (data[ 9] << 8 | data[10]) >> 3;
      Serial.printf("[CSTEST] cs=%3d #%d  x=%4d y=%4d | ", cs, rep + 1, x, y);
      for (int i = 8; i < 24; ++i) Serial.printf("%02X", data[i]);
      Serial.println();
    }
  }

  // 绕过 LovyanGFX 的过滤逻辑，直接把 XPT2046 的 7 组原始采样打出来。
  // 目的：把"SPI 链路根本没通"和"链路通了但标定不对"这两件事区分开。
  // LovyanGFX 的 getTouchRaw() 在采样不合格时一律返回 0，两种情况长得一模一样。
  // 返回落在有效区间里的采样点数（0..7）。
  int probeTouchRaw() const {
    auto *t = touch();
    if (!t) { Serial.println("[PROBE] no touch device"); return 0; }
    const auto c = t->config();

    uint8_t data[57];
    memset(data, 0, 8);
    data[ 0] = 0x91;  data[ 2] = 0xB1;  data[ 4] = 0xD1;  data[ 6] = 0xC1;
    data[56] = 0x80;                       // last power off
    memcpy(&data[ 8], data,  8);
    memcpy(&data[16], data, 16);
    memcpy(&data[32], data, 24);

    lgfx::spi::beginTransaction(c.spi_host, c.freq, 0);
    if (c.pin_cs > -1) lgfx::gpio_lo(c.pin_cs);
    lgfx::spi::readBytes(c.spi_host, data, 57);
    if (c.pin_cs > -1) lgfx::gpio_hi(c.pin_cs);
    lgfx::spi::endTransaction(c.spi_host);

    int ok = 0;
    for (int j = 0; j < 7; ++j) {
      const uint8_t *d = &data[j * 8];
      int x = (d[5] << 8 | d[6]) >> 3;
      int y = (d[1] << 8 | d[2]) >> 3;
      int z = 0x3200 + y - x + (((d[3] << 8 | d[4]) - (d[7] << 8 | d[8])) >> 1);
      bool xok = (x > 128 && x <= 3968);
      bool yok = (y > 128 && y <= 3968);
      if (xok && yok) ++ok;
      Serial.printf("[PROBE] %d/7 x=%4d%s y=%4d%s z=%5d | %02X %02X %02X %02X %02X %02X %02X %02X\n",
                    j + 1, x, xok ? "" : "!", y, yok ? "" : "!", z,
                    d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7]);
    }
    return ok;
  }
#else
  // 没有触摸的板子（profile 4）也要能编过：给一组空实现，
  // 这样上层代码不用到处写 #if HAS_TOUCH。
  void getTouchCal(uint16_t &, uint16_t &, uint16_t &, uint16_t &) const {}
  void setTouchCal(uint16_t, uint16_t, uint16_t, uint16_t) {}
  void rawToPanel(float, float, float &px, float &py) const { px = 0; py = 0; }
  void solveTouchCal(float, float, float, float, float, float, float, float,
                     uint16_t &, uint16_t &, uint16_t &, uint16_t &) const {}
  bool touchIrqEnabled() const { return false; }
  void setTouchIrq(bool) {}
  bool touchSoftSpi() const { return false; }
  void setTouchSoftSpi(bool) {}
  int probeTouchRaw() const { return 0; }
  void probeTouchCs(int) const {}
#endif

  // -------------------------------------------------------------------------
  //  读屏幕控制器的 ID（RDDID, 命令 0x04）
  //
  //  为什么需要：CYD 这一款板子出厂时装过两种屏 —— ILI9341 和 ST7789 ——
  //  LovyanGFX 为它们各写了一个识别器，而且两者的修正方式【不一样】：
  //    ILI9341 版：panel 的 offset_rotation = 2，touch 的 offset_rotation = 0
  //    ST7789  版：touch 的 offset_rotation = 2，panel 不动
  //  见 LGFX_AutoDetect_ESP32_all.hpp:3161-3173 和 :3195-3207。
  //  配错了画面就是歪的，而且看不出是"屏幕认错"还是"旋转写错"。
  //
  //  区分办法（同文件 :716-744 的 _read_panel_id，以及各识别器的 id_value）：
  //    ILI9341  RDDID 第一字节 = 0x00   （完整 00 93 41）
  //    ST7789V  RDDID 第一字节 = 0x85   （完整 85 85 52）
  //
  //  注意：不能用 tft.readPixel() 判断 MISO 通不通 —— LovyanGFX 的
  //  Panel_ILI9341 根本没有实现 readPixel，那个调用走的是基类兜底路径，
  //  必然返回垃圾。要测 MISO 就得像这里一样直接发命令读。
  // -------------------------------------------------------------------------
  void probeTftId() {          // 非 const：下面要调用 _bus_tft 的非 const 方法
    const int cs = PIN_TFT_CS;
    pinMode(cs, OUTPUT);

    // 完全照抄 LGFX_AutoDetect_ESP32_all.hpp:716-744 的 _read_panel_id：
    // 用 LovyanGFX 自己的 bus 对象发命令/收数据，这样 dummy bit 对齐、
    // DC 引脚切换、SPI 事务都由库来管，不会因为手搓 spi::readBytes
    // 的字节对齐问题读出一堆看似有数据、其实错位的字节。
    auto readId = [&](uint16_t cmd) -> uint32_t {
      const size_t dlen = 8;
      _bus_tft.beginTransaction();
      lgfx::gpio_hi(cs);
      _bus_tft.writeCommand(0, dlen);      // 先发一个空命令，让总线进入已知状态
      _bus_tft.wait();

      lgfx::gpio_lo(cs);
      _bus_tft.writeCommand(cmd, dlen);
      _bus_tft.beginRead(1);               // 1 个 dummy bit
      uint32_t res = 0;
      for (size_t i = 0; i < 4; ++i) {
        res |= (uint32_t)((_bus_tft.readData(dlen) >> (dlen - 8)) & 0xFF) << (i * 8);
      }
      _bus_tft.endTransaction();
      lgfx::gpio_hi(cs);
      return res;
    };

    uint32_t id = readId(0x04);
    Serial.printf("[TFTID] RDDID(0x04) = %02X %02X %02X %02X\n",
                  (unsigned)(id & 0xFF), (unsigned)((id >> 8) & 0xFF),
                  (unsigned)((id >> 16) & 0xFF), (unsigned)((id >> 24) & 0xFF));
    uint8_t id1 = id & 0xFF, id2 = (id >> 8) & 0xFF, id3 = (id >> 16) & 0xFF;
    Serial.printf("[TFTID] -> ");
    if (id1 == 0x00 && id2 == 0x93 && id3 == 0x41)
      Serial.println("ILI9341   => panel offset_rotation 应为 2, touch 保持 0");
    else if (id1 == 0x85 && id2 == 0x85 && id3 == 0x52)
      Serial.println("ST7789V   => touch offset_rotation 应为 2, panel 保持 0");
    else if (id == 0x00000000 || id == 0xFFFFFFFF)
      Serial.println("全 0 / 全 F —— MISO 没接、或屏没在驱动这条线");
    else
      Serial.println("不认识这个 ID");

    uint32_t st = readId(0x09);            // RDDST：交叉验证链路真的在回数据
    Serial.printf("[TFTID] RDDST(0x09) = %02X %02X %02X %02X\n",
                  (unsigned)(st & 0xFF), (unsigned)((st >> 8) & 0xFF),
                  (unsigned)((st >> 16) & 0xFF), (unsigned)((st >> 24) & 0xFF));
  }

  // -------------------------------------------------------------------------
  //  运行时改 offset_rotation（面板 / 触摸各一个）
  //
  //  存在的意义：offset_rotation 决定四个 rotation 各自对应哪个物理方向，
  //  配错就是"画面 180° 反 + 有一条边从没被写过"。它只能在 init 之前设，
  //  所以改完必须重新 init 面板 + 重新 setRotation 才生效。
  //  有了它就能不重新烧录、直接把几种组合试一遍。
  // -------------------------------------------------------------------------
  uint8_t panelOffsetRotation() const { return _panel.config().offset_rotation; }
  uint8_t touchOffsetRotation() const {
    auto *t = touch();
    return t ? t->config().offset_rotation : 0;
  }

  void setPanelOffsetRotation(uint8_t v) {
    auto c = _panel.config();
    c.offset_rotation = v & 7;
    _panel.config(c);
  }

  void setTouchOffsetRotation(uint8_t v) {
    auto *t = touch();
    if (!t) return;
    auto c = t->config();
    c.offset_rotation = v & 7;
    t->config(c);
  }

  // 改完 offset 之后让新值真正写进 MADCTL。
  //
  // ★ 这里**故意不调用 _panel.init()** ★ —— Panel_Device.inl:64-71 的
  //   bool Panel_Device::init(bool use_reset) {
  //     init_rst(); init_cs();
  //     if (_light) { _light->init(0); }      // <- 背光被重新 init 成 0！
  //     ...
  //   会在重新 init 面板时把背光亮度清成 0，屏幕立刻变成"全黑但还能点"。
  //   setRotation() 自己就会重算 colstart/rowstart 并调用 update_madctl()，
  //   所以重写 MADCTL 根本不需要重新跑一遍面板 init 序列。
  void reinitPanel(uint8_t rotation) {
    setRotation(rotation);
  }

  // 屏幕坐标 -> "面板原始方向"空间。
  //
  // 为什么需要：标定值 x_min/x_max/y_min/y_max 定义在【面板原始方向】上
  // （Panel_Device::setCalibrate 用 _cfg.panel_width/panel_height 建矩阵），
  // 而用户是在【旋转之后的屏幕】上点靶心。所以标定向导必须先做一次逆变换，
  // 才知道哪个靶心对应 x_min、哪个对应 x_max。
  //
  // 这段是 Panel_Device::convertRawXY()（Panel_Device.inl:503-525）的严格逆运算：
  //   正向: swap(if r&1) -> tx=(W-1)-tx(if r&2) -> ty=(H-1)-ty(if vflip)
  //   逆向: 反过来、倒着做
  // r 的算法照抄 Panel_LCD::setRotation（Panel_LCD.inl:128-133）再叠上触摸自己的
  // offset_rotation，等价于 convertRawXY 里的 _internal_rotation。
  void screenToPanelSpace(int sx, int sy, int &px, int &py) const {
    uint8_t r  = getRotation() & 7;
    uint8_t po = panel()->config().offset_rotation;
    uint8_t ir = ((r + po) & 3) | ((r & 4) ^ (po & 4));
    if (auto *t = touch()) {                 // 用 auto：ITouch 这个名字在本作用域取不到
      uint8_t to = t->config().offset_rotation;
      ir = ((ir + to) & 3) | ((ir & 4) ^ (to & 4));
    }
    bool vflip = (1 << ir) & 0b10010110;     // ir = 1,2,4,7 时 Y 要翻
    int a = sx, b = sy;
    if (vflip) b = (height() - 1) - b;
    if (ir & 2) a = (width()  - 1) - a;
    if (ir & 1) { int tmp = a; a = b; b = tmp; }
    px = a; py = b;
  }
};

extern LGFX tft;
