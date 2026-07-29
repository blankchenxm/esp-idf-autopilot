MCU芯片为ESP32-PICO-D4

BQ25180YBGR充电芯片：scl_pin连接gpio引脚27，sda_pin连接gpio引脚14

W25N01GV flash芯片：clk_pin连接gpio引脚18，mosi_pin连接gpio引脚23，miso_pin连接gpio引脚19，cs_pin连接gpio引脚5

两个microphone：两个microphone共享i2s接口，其中clk_pin连接gpio 26，data_pin连接gpio25，左右通道已经在硬件上自动连接了，一个mic select引脚连接GND，一个连接了VDD3.3

BUTTON连接的是gpio引脚4，active_level: low

BQ25180YBGR I2C transport policy: controller 0, 400 kHz.
