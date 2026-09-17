import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Search } from 'lucide-react';
import { EmptyState } from '../components/ui';
import { langPairLabel } from './lang-labels';

function timeLabel(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number): string => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 跨会话搜索（session-history spec：原文+译文、分组结果、跳转定位） */
export function SearchPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const initial = searchParams.get('q') ?? '';
  const [query, setQuery] = useState(initial);
  const [hits, setHits] = useState<SearchHitView[] | null>(null);
  const [searching, setSearching] = useState(false);

  const runSearch = (q: string): void => {
    const trimmed = q.trim();
    if (!trimmed) {
      setHits(null);
      return;
    }
    setSearching(true);
    window.appAPI
      .searchHistory(trimmed)
      .then(setHits)
      .catch((e: unknown) => console.error('搜索失败', e))
      .finally(() => setSearching(false));
  };

  useEffect(() => {
    if (initial) runSearch(initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 按会话分组
  const groups: Array<{ title: string; startedAt: number; sessionId: string; items: SearchHitView[] }> = [];
  for (const hit of hits ?? []) {
    const last = groups[groups.length - 1];
    if (last && last.sessionId === hit.utterance.session_id) {
      last.items.push(hit);
    } else {
      groups.push({
        title: hit.sessionTitle,
        startedAt: hit.sessionStartedAt,
        sessionId: hit.utterance.session_id,
        items: [hit]
      });
    }
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-12 shrink-0 items-center gap-2 border-b border-edge px-6">
        <Search className="h-4 w-4 text-secondary" />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              setSearchParams(query.trim() ? { q: query.trim() } : {}, { replace: true });
              runSearch(query);
            }
          }}
          placeholder="搜索原文或译文，回车执行…"
          autoFocus
          className="h-8 w-full max-w-xl bg-transparent text-base text-primary placeholder:text-secondary focus:outline-none"
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-8 py-6">
        {hits === null && !searching && (
          <EmptyState
            icon={<Search className="h-8 w-8" />}
            title="搜索全部历史字幕"
            description="按原文或译文匹配（不区分大小写），结果按会话分组。"
          />
        )}
        {searching && <p className="text-center text-sm text-secondary">搜索中…</p>}
        {hits !== null && !searching && hits.length === 0 && (
          <EmptyState title="没有匹配结果" description="换个关键词试试。" />
        )}

        {groups.map((g) => (
          <div key={g.sessionId} className="mb-6">
            <button
              type="button"
              className="mb-2 flex items-baseline gap-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-control"
              onClick={() => navigate(`/session/${g.sessionId}`)}
            >
              <span className="text-base font-semibold text-primary">{g.title}</span>
              <span className="text-xs text-secondary opacity-70">
                {timeLabel(g.startedAt)} · {g.items.length} 处命中
              </span>
            </button>
            <div className="rounded-card border border-edge bg-sidebar">
              {g.items.map((hit) => (
                <button
                  key={hit.utterance.id}
                  type="button"
                  onClick={() => navigate(
                    `/session/${hit.utterance.session_id}?focus=${hit.utterance.id}`
                  )}
                  className="flex w-full flex-col gap-0.5 border-b border-edge px-4 py-3 text-left transition-colors duration-fast ease-app last:border-b-0 hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <span className="text-base text-primary">{hit.snippet}</span>
                  <span className="text-xs text-secondary opacity-70">
                    {hit.matchedField === 'translation' ? '命中译文' : '命中原文'}
                    {hit.utterance.source_lang && hit.utterance.target_lang
                      ? ` · ${langPairLabel(hit.utterance.source_lang, hit.utterance.target_lang)}`
                      : ''}
                  </span>
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
