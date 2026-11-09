/** 外观设置：背景预设/自定义背景/动效档位。localStorage 持久化，变更即生效。 */

import { readBrand, writeBrand } from "./brand";

export type MotionLevel = "full" | "soft" | "off";

export interface Appearance {
  /** 背景预设：theme 跟随主题，其余为装饰背景 */
  bg: string;
  /** 自定义纯色（bg=solid 时生效） */
  bgColor: string;
  /** 自定义图片 URL（bg=image 时生效） */
  bgImage: string;
  motion: MotionLevel;
}

const KEY = "appearance";

export const BG_PRESETS: [string, string][] = [
  ["theme", "跟随主题"],
  ["aurora", "极光"],
  ["mesh", "弥散"],
  ["grid", "网格"],
  ["dots", "波点"],
  ["waves", "波纹"],
  ["solid", "纯色"],
  ["image", "图片"],
];

export const MOTION_LEVELS: [MotionLevel, string][] = [
  ["full", "丰富（环境漂移 + 入场动效）"],
  ["soft", "柔和（仅微交互）"],
  ["off", "关闭（省电/无障碍）"],
];

const DEFAULTS: Appearance = {
  bg: "theme",
  bgColor: "#151b2c",
  bgImage: "",
  motion: "soft",
};

export function loadAppearance(): Appearance {
  try {
    const raw = readBrand(KEY);
    if (raw) return { ...DEFAULTS, ...(JSON.parse(raw) as Partial<Appearance>) };
  } catch {
    /* 坏数据回退默认 */
  }
  return { ...DEFAULTS };
}

export function saveAppearance(a: Appearance): void {
  try {
    writeBrand(KEY, JSON.stringify(a));
  } catch {
    /* 隐私模式等存储失败不阻塞 */
  }
}

export function applyAppearance(a: Appearance = loadAppearance()): void {
  const root = document.documentElement;
  root.setAttribute("data-bg", a.bg);
  root.setAttribute("data-motion", a.motion);
  root.style.setProperty("--user-bg-color", a.bgColor || "transparent");
  root.style.setProperty(
    "--user-bg-image",
    a.bgImage ? `url("${a.bgImage.replace(/["\\)]/g, "")}")` : "none"
  );
}
