/** 品牌星标：三颗四点星，取自排版符号 ⁂（asterism，星群）。
 *
 *  同一个形状出现在 favicon、应用头 logo 与铭文页；星数固定为三——one above, two below，
 *  和 ⁂ 的字形一致。填充色跟随 currentColor，明暗主题自动适配。 */

export default function StarMark({ size = 18 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="currentColor"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M32 19.9 Q33.9 25.1 39.1 27 Q33.9 28.9 32 34.1 Q30.1 28.9 24.9 27 Q30.1 25.1 32 19.9 Z" />
      <path d="M23.4 30.4 Q25.3 35.6 30.5 37.5 Q25.3 39.4 23.4 44.6 Q21.5 39.4 16.3 37.5 Q21.5 35.6 23.4 30.4 Z" opacity="0.92" />
      <path d="M40.6 30.4 Q42.5 35.6 47.7 37.5 Q42.5 39.4 40.6 44.6 Q38.7 39.4 33.5 37.5 Q38.7 35.6 40.6 30.4 Z" opacity="0.92" />
    </svg>
  );
}
