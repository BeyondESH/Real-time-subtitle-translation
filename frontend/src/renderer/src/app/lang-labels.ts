/**
 * 语言展示标签（仅 UI 文案映射；选择逻辑一律以消息/配置数据为准，
 * 不违反 subtitle-display spec "前端 MUST NOT 硬编码语言代码" 的行为约束）
 */
const LABELS: Record<string, string> = {
  zh: '中',
  en: '英',
  ja: '日',
  ko: '韩',
  fr: '法',
  de: '德',
  es: '西',
  ru: '俄',
  it: '意',
  pt: '葡',
  ar: '阿',
  hi: '印地',
  th: '泰',
  vi: '越',
  id: '印尼'
};

export function langLabel(code: string): string {
  return LABELS[code] ?? code;
}

export function langPairLabel(source: string, target: string): string {
  return `${langLabel(source)}→${langLabel(target)}`;
}
