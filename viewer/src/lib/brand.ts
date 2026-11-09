/** 品牌常量与本地存储迁移。
 *
 *  名 Lodestar · 礼 Catasterism · 符号 ⁂（asterism，星群）
 *  中文名 星引 —— 星的引进，也是引你到星。
 *
 *  改名前的 localStorage 键前缀是 `note-gen-`，改名后统一走 `lodestar-`。
 *  读取一律走 readBrand：新键优先，回退旧键并就地迁移，用户的主题/栏宽/缩放偏好不丢。
 */

export const BRAND = {
  en: "Lodestar",
  zh: "星引",
  mark: "⁂",
  tagline: "以书为图，以记为星",
  version: "4.0.4",
  /** 罗马数字 404：CD(400) + IV(4)。只出现在版本行与铭文，不上主标。 */
  roman: "CDIV",
  versionLabel: "4.0.4 · CDIV",
} as const;

const PREFIX = "lodestar-";
const LEGACY_PREFIX = "note-gen-";

export function brandKey(suffix: string): string {
  return PREFIX + suffix;
}

/** 启动时跑一次：把旧前缀的键复制到新前缀（新键已存在则以新键为准）。 */
export function migrateBrandStorage(): void {
  try {
    const legacy: string[] = [];
    for (let i = 0; i < localStorage.length; i += 1) {
      const k = localStorage.key(i);
      if (k && k.startsWith(LEGACY_PREFIX)) legacy.push(k);
    }
    for (const k of legacy) {
      const next = PREFIX + k.slice(LEGACY_PREFIX.length);
      if (localStorage.getItem(next) === null) {
        const v = localStorage.getItem(k);
        if (v !== null) localStorage.setItem(next, v);
      }
    }
  } catch {
    /* 隐私模式：存储不可用时静默 */
  }
}

export function readBrand(suffix: string, fallback: string | null = null): string | null {
  try {
    const next = localStorage.getItem(PREFIX + suffix);
    if (next !== null) return next;
    const legacy = localStorage.getItem(LEGACY_PREFIX + suffix);
    if (legacy !== null) {
      localStorage.setItem(PREFIX + suffix, legacy);
      return legacy;
    }
  } catch {
    /* 忽略 */
  }
  return fallback;
}

export function writeBrand(suffix: string, value: string): void {
  try {
    localStorage.setItem(PREFIX + suffix, value);
  } catch {
    /* 忽略 */
  }
}
