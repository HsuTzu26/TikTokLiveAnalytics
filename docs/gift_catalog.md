# Gift ID Catalog

Gift ID is the primary key. The catalog stores TikTok original names, English names, Chinese display names, TTS pronunciation names, observed diamond counts, mapping status, and field sources separately. Event fields `gift_name` and `gift_name_original` keep TikTok's exact incoming text; translation and speech cleanup do not modify them.

The catalog contains 682 Gift IDs from the public STANDOUT Streamers catalog snapshot and 78 Gift IDs observed in saved Browser Network sessions (702 unique IDs total). The source page's embedded list was updated at `2026-09-29T05:59:13Z`; 20 observed IDs were not present in that page snapshot. Uncertain names stay as received and are marked for review.

`diamond_count` is observed in `WebcastGiftMessage.gift.diamond_count`. Per-event `diamond_total` remains the source of truth for LIVE summaries. TikTok values may vary by room or region.

The imported `coin_price` is the viewer purchase cost reported by the public catalog. It is stored separately from `diamond_count`, which is populated only from observed LIVE events. Gift image URLs are stored for the local editor preview; the images are not downloaded or cached. Source names and coin prices can be refreshed with `python scripts/update_standout_gift_catalog.py`.

Chinese display and TTS names remain pending until verified. The site's English label is kept in `standout_name` and used to fill a missing English name when it is ASCII. The existing raw event name, translated name, pronunciation name, and event diamond count remain separate.

Run `scripts/start_gift_catalog_editor.bat` to open the local visual editor. It shows gift thumbnails, IDs, names, and coin prices. Filter by source and switch between saved translations, pending translations, or all rows; then edit the display and TTS names and save directly to the catalog. Repeated names under different IDs remain separate because Gift ID is the key. Manual entries are marked with a `manual_translation` source.

To import a completed UTF-8 TSV list such as `data/gift_catalog_zh_TW.txt`, run `python scripts/import_gift_translations.py`. The importer matches each row by Gift ID and verifies its English name, Coin value, and image URL before applying the two Chinese fields.

## Gift IDs observed in saved LIVE sessions

The following table shows only the 78 locally observed IDs. The JSON catalog also contains all 682 IDs in the public source snapshot.

| Gift ID | Exact TikTok original name | English name | Chinese display / TTS | Observed diamonds | Status |
|---:|---|---|---|---:|---|
| 5269 | `"TikTok"` | TikTok | TikTok | 1 | mapped |
| 5487 | `"Finger Heart"` | Finger Heart | 比心 | 5 | mapped |
| 5586 | `"Hearts"` | Hearts | 愛心 | 199 | mapped |
| 5655 | `"Rose"` | Rose | 玫瑰花 | 1 | mapped |
| 5659 | `"Paper Crane"` | Paper Crane | 紙鶴 | 99 | mapped |
| 5660 | `"Hand Heart"` | Hand Heart | 手比愛心 | 100 | mapped |
| 5879 | `"Doughnut"` | Doughnut | 甜甜圈 | 30 | mapped |
| 5897 | `"Swan"` | Swan | 天鵝 | 699 | mapped |
| 6064 | `"GG"` | GG | GG | 1 | mapped |
| 6104 | `"Cap"` | Cap | 帽子 | 99 | mapped |
| 6149 | `"Interstellar"` | Interstellar | 星際 | 10000 | mapped |
| 6233 | `"Travel with You"` | Travel with You | 與你同行 | 999 | mapped |
| 6267 | `"Corgi"` | Corgi | 柯基犬 | 299 | mapped |
| 6427 | `"Hat and Mustache"` | Hat and Mustache | 帽子與鬍子 | 99 | mapped |
| 6646 | `"Leon the Kitten"` | Leon the Kitten | 里昂小貓 | 4888 | mapped |
| 6784 | `"Cake Slice"` | Cake Slice | 蛋糕切片 | 1 | mapped |
| 6788 | `"Glow Stick"` | Glow Stick | 螢光棒 | 1 | mapped |
| 6820 | `"Whale diving"` | Whale diving | 鯨魚潛水 | 2150 | mapped |
| 7168 | `"Money Gun"` | Money Gun | 鈔票槍 | 500 | mapped |
| 7823 | `"Leon and Lion"` | Leon and Lion | 里昂與獅子 | 34000 | mapped |
| 7934 | `"Heart Me"` | Heart Me | 愛心 | 1 | mapped |
| 8913 | `"Rosa"` | Rosa | 羅莎 | 10 | mapped |
| 8914 | `"Forever Rosa"` | Forever Rosa | 永恆玫瑰 | 399 | mapped |
| 9500 | `"Flying Jets"` | Flying Jets | 飛行噴射機 | 5000 | mapped |
| 10576 | `"Fried Chicken"` | Fried Chicken | 炸雞 | 10 | mapped |
| 10669 | `"Future City"` | Future City | 未來之城 | 6000 | mapped |
| 11046 | `"Galaxy"` | Galaxy | 銀河 | 1000 | mapped |
| 11180 | `"Love Painting"` | Love Painting | 愛情畫作 | 99 | mapped |
| 11586 | `"Party On&On"` | Party On&On | 派對狂歡 | 15000 | mapped |
| 11712 | `"Twinkling Star"` | Twinkling Star | 閃耀星星 | 199 | mapped |
| 11919 | `"Pork Rice Bowl"` | Pork Rice Bowl | 滷肉飯 | 10 | mapped |
| 12355 | `"Team Cheers"` | Team Cheers | 團隊歡呼 | 1 | mapped |
| 12356 | `"Team Power"` | Team Power | 團隊力量 | 9 | mapped |
| 12357 | `"Team Victory"` | Team Victory | 團隊勝利 | 99 | mapped |
| 13352 | `"Rose Carriage"` | Rose Carriage | 玫瑰馬車 | 25000 | mapped |
| 13460 | `"Mini star"` | Mini star | 小星星 | 100 | mapped |
| 13651 | `"Popular Vote"` | Popular Vote | 人氣投票 | 1 | mapped |
| 13895 | `"Cupid's Bow"` | Cupid's Bow | 丘比特之弓 | 99 | mapped |
| 14032 | `"Reunion Hotpot"` | Reunion Hotpot | 團圓火鍋 | 100 | mapped |
| 14223 | `"Match Wand"` | Match Wand | 對戰魔法棒 | 100 | mapped |
| 15064 | `"Racing Helmet"` | Racing Helmet | 賽車安全帽 | 500 | mapped |
| 15231 | `"Love you so much"` | Love you so much | 好愛你 | 1 | mapped |
| 15232 | `"You're awesome"` | You're awesome | 你真棒 | 1 | mapped |
| 16478 | `"Bubble Headphones"` | Bubble Headphones | 泡泡耳機 | 249 | mapped |
| 16552 | `"Heart Balloons"` | Heart Balloons | 愛心氣球 | 149 | mapped |
| 16690 | `"Super Popular"` | Super Popular | 超人氣 | 9 | mapped |
| 17085 | `"Music Album"` | Music Album | 音樂專輯 | 1 | mapped |
| 17105 | `"Sundae Bowl"` | Sundae Bowl | 聖代碗 | 99 | mapped |
| 17360 | `"Chirpy Kisses"` | Chirpy Kisses | 啾啾飛吻 | 199 | mapped |
| 17922 | `"Charmer Bow"` | Charmer Bow | 魅力蝴蝶結 | 99 | mapped |
| 17923 | `"Joker Ball"` | Joker Ball | 小丑球 | 199 | mapped |
| 17948 | `"Joy Floats"` | Joy Floats | 歡樂漂浮 | 1030 | mapped |
| 17986 | `"Raving Snail"` | Raving Snail | 狂歡蝸牛 | 149 | mapped |
| 19308 | `"Sweet Flutter"` | Sweet Flutter | 甜蜜心動 | 249 | mapped |
| 29932 | `"堅定同行"` | pending | 堅定同行 | 1 | needs review: name_en |
| 31408 | `"安室日记"` | pending | 安室日記 | 1 | needs review: name_en |
| 38860 | `"苡家小猪"` | pending | 苡家小豬 | 1 | needs review: name_en |
| 46674 | `"Crossette Firework"` | Crossette Firework | 十字煙火 | 129 | mapped |
| 48224 | `"Fairy Hide"` | Fairy Hide | 仙女躲藏 | 149 | mapped |
| 54566 | `"Snow Bloom"` | Snow Bloom | 雪花綻放 | 249 | mapped |
| 57009 | `"Wakey Mallow"` | Wakey Mallow | 醒醒棉花糖 | 299 | mapped |
| 57594 | `"Cheeky Pup"` | Cheeky Pup | 調皮小狗 | 400 | mapped |
| 60549 | `"Craft Dreamer"` | Craft Dreamer | 手作夢想家 | 349 | mapped |
| 81427 | `"愛哭波"` | pending | 愛哭波 | 1 | needs review: name_en |
| 231955 | `"Good Job"` | Good Job | 做得好 | 1 | mapped |
| 231956 | `"Clap Clap"` | Clap Clap | 拍拍手 | 1 | mapped |
| 637990 | `"Treasure Clover"` | Treasure Clover | 幸運三葉草 | 1 | mapped |
| 893970 | `"悲伤一暝又一暝"` | pending | 悲傷一暝又一暝 | 199 | needs review: name_en |
| 991838 | `"碎碎唸"` | pending | 碎碎唸 | 1 | needs review: name_en |
| 1241064 | `"Ray Serenade "` | Ray Serenade | 魟魚小夜曲 | 2999 | mapped |
| 1266755 | `"Autumn heart"` | Autumn heart | 秋日之心 | 1 | mapped |
| 1267090 | `"Full moon"` | Full moon | 滿月 | 299 | mapped |
| 1298057 | `"Glow Chant"` | Glow Chant | 螢光應援 | 450 | mapped |
| 1315303 | `"Chada Ansu"` | Chada Ansu | 恰達安蘇 | 199 | mapped |
| 1339422 | `"Oh my love"` | Oh my love | 我的愛 | 199 | mapped |
| 1359368 | `"Sunset Cheer"` | Sunset Cheer | 日落歡呼 | 1600 | mapped |
| 1359369 | `"Flight Ticket"` | Flight Ticket | 機票 | 30 | mapped |
| 1359371 | `"Live Vacation"` | Live Vacation | 直播假期 | 449 | mapped |

## Sources and update rules

- Original names and diamond counts come from captured TikTok LIVE `WebcastGiftMessage` events. The JSON stores observation counts without including streamer names or login details.
- English names are copied only when TikTok supplied an English original. Localized originals without a reliable English mapping remain pending.
- `name_zh_display` and `name_zh_tts` are independent fields. They can share a value now while allowing pronunciation aliases to change without changing the display label.
- New Gift IDs keep their raw names and event diamond values. Unknown names appear in the per-session report with their ID, original name, and pending fields.
- TTS pronunciation overrides and custom sound mappings use Gift ID, so renaming a gift cannot detach its sound.
- Mixed-language cleanup happens after event decoding. Pathological script alternation preserves the complete text and falls back to one dominant-language TTS segment.
- The worker and launcher use Python UTF-8 mode; event and delivery NDJSON files are explicitly written as UTF-8.

Data file: [`src/tts/gift_catalog.json`](../src/tts/gift_catalog.json).
