/**
 * 翻译模型列表共享逻辑（设置页"模型"分段翻译区块）。
 *
 * 经 WS `get_config` 拉取后端注册表摘要与当前实际模型（settings-management spec
 * 「翻译模型设置与状态显示」）；与框架无关、可单测。
 * 旧后端缺 `translation.model` / `available_models` 字段时按缺失容错，绝不报错。
 */
import type { WsResponse } from '../../../shared/ipc-types';

export interface TranslationModelInfo {
  id: string;
  display_name: string;
  size_bytes: number;
  downloaded: boolean;
  current: boolean;
}

export interface TranslationModelsData {
  /** 后端当前实际模型 id；null=旧后端/字段缺失/未连接 */
  model: string | null;
  /** 注册表摘要；null=旧后端/字段缺失 */
  available: TranslationModelInfo[] | null;
}

export type WsRequestFn = (method: string, params?: unknown) => Promise<WsResponse>;

function defaultRequest(method: string, params?: unknown): Promise<WsResponse> {
  return window.appAPI.wsRequest(method, params);
}

function parseModelInfo(raw: unknown): TranslationModelInfo | null {
  if (!raw || typeof raw !== 'object') return null;
  const rec = raw as Record<string, unknown>;
  if (typeof rec.id !== 'string' || rec.id === '') return null;
  return {
    id: rec.id,
    display_name: typeof rec.display_name === 'string' && rec.display_name
      ? rec.display_name
      : rec.id,
    size_bytes: typeof rec.size_bytes === 'number' && rec.size_bytes > 0
      ? rec.size_bytes
      : 0,
    downloaded: rec.downloaded === true,
    current: rec.current === true
  };
}

/** 拉取并解析 get_config 的 translation 段（形状非法/请求失败按异常抛出，调用方呈现错误态） */
export async function fetchTranslationModels(
  request: WsRequestFn = defaultRequest
): Promise<TranslationModelsData> {
  const resp = await request('get_config');
  if (!resp.ok) throw new Error(resp.message || resp.error);
  const result = (resp.result !== null && typeof resp.result === 'object'
    ? resp.result
    : {}) as Record<string, unknown>;
  const tr = (result.translation !== null && typeof result.translation === 'object'
    ? result.translation
    : {}) as Record<string, unknown>;
  const model = typeof tr.model === 'string' && tr.model !== '' ? tr.model : null;
  const rawList = Array.isArray(tr.available_models) ? tr.available_models : null;
  const available = rawList
    ? rawList.map(parseModelInfo).filter((m): m is TranslationModelInfo => m !== null)
    : null;
  return { model, available };
}

/** 体积展示（MB/GB） */
export function formatModelSize(bytes: number): string {
  if (!bytes || bytes <= 0) return '';
  const gb = bytes / 1024 / 1024 / 1024;
  if (gb >= 1) return `${gb.toFixed(1)}GB`;
  return `${Math.round(bytes / 1024 / 1024)}MB`;
}

export interface TranslationSelectOption {
  value: string;
  label: string;
}

/**
 * Select 选项：注册表列表（标注体积与下载状态）+ 保证 store 当前值在列，
 * 避免后端未连接/旧后端时下拉误导性回落到首项。
 */
export function buildTranslationSelectOptions(
  data: TranslationModelsData | null, storeModel: string
): TranslationSelectOption[] {
  const list = data?.available ?? [];
  const options: TranslationSelectOption[] = list.map((m) => ({
    value: m.id,
    label: `${m.display_name}`
      + (m.size_bytes > 0 ? ` · ${formatModelSize(m.size_bytes)}` : '')
      + (m.downloaded ? '' : '（未下载）')
  }));
  if (storeModel !== '' && !options.some((o) => o.value === storeModel)) {
    options.unshift({ value: storeModel, label: `${storeModel}（当前选择）` });
  }
  return options;
}

/**
 * 状态行文案：以【后端实际模型】为准；与 store 偏好不一致（启动对齐/切换中）
 * 呈现加载态，MUST NOT 以偏好值冒充实际模型（settings-management spec）。
 */
export function translationModelStatusText(
  data: TranslationModelsData | null, storeModel: string
): string {
  if (!data || data.model === null) return '正在检测翻译模型…';
  if (data.model !== storeModel) return '正在加载/对齐中…';
  const hit = data.available?.find((m) => m.id === data.model);
  return `当前使用：${hit?.display_name ?? data.model}`;
}
