# Lodestar 品牌手册

> 名 **Lodestar**（引路之星）· 中文 **星引** · 礼 **Catasterism**（列入星空）· 符号 **⁂**（asterism，星群）
> 一句话：**以书为图，以记为星**。英文副标：*Every note, a fixed star.*

完整的推演过程（为什么是 Lodestar、为什么 Catasterism 只当礼、为什么拉丁文只做铭）见
[brand-book.md](brand-book.md)。

---

## 一、名字藏在哪里

对外是一个天文词，对内是四个暗号：

| 名字 | 藏处 | 藏法 |
| --- | --- | --- |
| 洛 | `Lo`dstar | 读音就是「洛」；拼写上只是两个不起眼的字母 |
| 星 | lo**dstar** | 明写，但读者只当它是个天文词 |
| Lstar | 首字母 **L** | 大写排出来就是 `L★` |
| 404 | 版本 **4.0.4 · CDIV** | 罗马数字 CD(400) + IV(4) ＝ 404 |
| 猫 / 喵 | **Felis** | 1799 年拉朗德造的猫座，后被 IAU 废除；但它的最亮星 HD 85951 在 2018 年被正式命名为 Felis——星座没了，星还叫这个名字 |

## 二、三层用法

| 层 | 内容 | 出现在哪 |
| --- | --- | --- |
| 名 | Lodestar / 星引 | 界面字标、标题、仓库名、包名 |
| 礼 | Catasterism / catasterize | 大版本代号、笔记并入导图的那一刻、"全书写完" 的仪式提示 |
| 铭 | CDIV、碑刻大写、拉丁词汇表 | 版本行、封面扉页、导出文件头、命令行——**不上界面主干** |

## 三、硬规则

1. **界面字标** ＝ ⁂ 图标（28px 圆角方块内 20px）＋ `Lodestar`（16px 现代衬线）＋ `星引`（11.5px，字距 .32em）。
2. **碑刻大写只吃大尺寸**：全大写＋宽字距只用于封面、扉页、大标题；16px 以下会散、会虚。
3. **CDIV 不上主标第一行**：会被读成 `CD + IV`，或误认成光盘。
4. **⁂ 一符两用**：既是品牌符号（asterism 星群），也是笔记分节符——天天出现，等于天天签名。
5. **拉丁词汇只做暗号**：只出现在提示、导出文件头、命令行，不翻译也不进导航栏。
   - `stella` 一颗星＝一条笔记　`sidus` 星座＝一张导图　`via` 路径＝阅读顺序
   - `feles` 猫＝AI 助手　`catasterismus` 列入星空＝成图
6. **`.ngshare` 的格式标识 `note-gen-share` 冻结**：它是文件格式契约，改了旧分享包读不了。
7. **localStorage 前缀 `lodestar-`**：旧的 `note-gen-*` 由 `viewer/src/lib/brand.ts` 的
   `migrateBrandStorage()` 在启动时一次性迁移，用户偏好不丢。

## 四、资产

| 文件 | 用途 |
| --- | --- |
| [lodestar-mark.svg](lodestar-mark.svg) | 主标：64×64，圆角 16，深松绿底＋奶油三颗四点星 |
| [preview.html](preview.html) | 品牌页：明/暗应用头、分节线、404 页、铭文层——双击用浏览器打开 |
| [app-light.png](app-light.png) / [app-dark.png](app-dark.png) | 真实界面截图（字标落地效果） |
| [inscription-06.png](inscription-06.png) | 铭文层细节：`LODESTAR ⁂ CDIV` ＋ `AD ASTRA · MMXXVI` |

配色：主标绿 `#0E5A46`（与界面 accent 同族），星色奶油 `#F3EEE2`。
字形：字标走 Georgia / Times New Roman 一路的衬线；正文 Segoe UI / 微软雅黑。

## 五、改名前的人

原名 `note-gen`。改名只换皮，不换数据：仓库目录、导出格式、Anki 稳定 id、数据目录布局全部保持兼容，
用户升级后主题、栏宽、PDF 缩放偏好与已读引导状态都还在。
