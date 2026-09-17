/** 类名拼接工具：过滤 falsy，空格连接 */
export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ');
}
