芯片为esp32-pico-d4，sensor有一个BQ25180YBGR充电芯片，一个W25N01GV flash芯片，两个ICS-41350 micophone，以及一个gpio控制的button元件。

逻辑为：对于充电芯片，它可以通过 USB 连接口给一个锂电池进行充电。充电芯片可以观察充电时的一些配置情况；对于 ESP32 芯片，它会连接 WiFi。核心逻辑是：用户在每次按下按钮到松开按钮的这段时间内，会通过两个麦克风元件进行录音处理，其中，两个麦克风各占左右声道，并将这段音频存储到 Flash 芯片上。由于 ESP32 连接了 WiFi，它的逻辑是通过 WiFi 将音频发送到一个服务器的网址上。当有 WiFi 时，它会根据 Flash 里面存储的音频，按时间由远到近(最旧优先)，将音频一个一个上传，上传后再进行删除。没有wifi时则持续录音。上传的地址为`http://crumb.xiaomeng-research.xyz/upload`

此外，我希望 WiFi 名称和密码是存储在 ESP32 的 NVS 里面。当 ESP32 读取时，逻辑如下：
1. 如果已经存有 WiFi 信息，则直接使用该 WiFi 联网
2. 如果没有相关信息（例如第一次运行时），则将 WiFi 名称和密码写入 NVS

具体wifi信息为：
wifi ssid:TP-LINK_cjx
wifi password:chen19720530

并且每段音频命名用真实时间 `rec_YYYYMMDD-HHMMSS_<duration_sec>s`，这可以通过wifi连接后NTP实现。因为无RTC硬件，若启动后尚未完成首次 NTP 同步，音频先用临时名 `rec_BOOT_<uptime_ms>_<dur>s`

补充产品决策：
1. BQ25180YBGR 仅允许只读观测；固件不得写入或修改任何充电配置。
2. W25N01GV 存储空间满时，删除最早的已保存音频以容纳新的录音；删除必须以完整录音为单位，不能覆盖或截断正在写入/上传的文件。
3. 音频使用 ICS-41350 输出的 I2S PCM 双声道数据，并封装为 WAV 后存储和上传。采样率、位宽、单段最大时长和允许丢样阈值必须以已验证的 ICS-41350 数据手册/ESP-IDF I2S 支持为准；在获得这些依据前不得猜测具体数值。
4. 上传协议以提供的 FastAPI 服务端为准：对 `http://crumb.xiaomeng-research.xyz/upload` 发起 HTTP POST，请求体为完整 WAV 文件的原始字节，文件名以 `filename` 查询参数传递；服务端不要求认证。仅当响应为 HTTP 200 且 JSON 响应中的 `bytes` 等于本次上传文件字节数、并包含非空 `saved` 字段时，才允许删除本地录音。服务端单文件上限为 50 MiB；其他状态码、网络错误、响应解析失败或字节数不匹配均视为上传失败并保留本地文件。
