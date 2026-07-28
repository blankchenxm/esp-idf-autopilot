# hm01b0_st7789 Design Draft

Build ESP-IDF firmware for an ESP32-S3-WROOM-1 that captures frames from the exact HM01B0-MWA camera and continuously displays them through the exact ST7789 controller. The design preserves every declared connection: HM01B0 MCLK/PCLK/VSYNC/HSYNC on GPIO5/6/7/8, D0-D7 on GPIO9-GPIO16, camera I2C SCL/SDA on GPIO1/GPIO2, and ST7789 SCL/SDA/RES/DC/CS on GPIO35-GPIO39. The display backlight remains directly connected to 3.3 V and is not controlled by firmware.

The firmware uses ESP-IDF facilities, bounded DMA-capable buffers where required, a depth-two latest-frame-wins queue, separate capture/render/control responsibilities, bounded recovery, and explicit runtime telemetry. Camera mode, panel geometry, offsets, orientation, color order, transfer timing, and operation-level register details must come from Registry, authoritative datasheet, installed ESP-IDF, and hardware-validation evidence; no ungrounded hardware value is frozen here.

Acceptance requires a live pipeline, bounded transfers, no buffer/address-window overflow, correct dependency-order startup, and at least 5 displayed frames per second for 30 seconds with zero reported buffer overruns, transport errors, or application restarts. A final Tier C observation confirms that the connected physical display presents a live, stable, correctly oriented image for 30 seconds. Registry searches conclude with explicit custom-driver selections for both external devices; those decisions do not waive the outstanding authoritative acquisition and implementation-readiness work recorded below.

## Harness grounding reconciliation and readiness

The following provider statements are not open product decisions. Their authoritative handling is recorded here:

- Acquire and bind the exact HM01B0-MWA Registry search receipt and authoritative L1 datasheet evidence; obtain missing L2/L3 operation facts during execution readiness before implementing or accessing hardware.

- Acquire and bind the exact ST7789 Registry search receipt and authoritative controller/module evidence for geometry, offsets, orientation, color order, timing, and safety; obtain missing L2/L3 operation facts during execution readiness.

- Bind installed ESP-IDF API and capability facts for the selected parallel capture, SPI, I2C, GDMA, and DMA-capable memory implementation before build and hardware access.

