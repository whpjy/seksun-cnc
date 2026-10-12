/** Bounded display-only queue: coalesce stream deltas, prioritize newest rows. */
export function needsTranslation(text: string): boolean {
  const prose = text.replace(/`[^`]*`|https?:\/\/\S+|\b[\w]+_[\w]+\b/g, "");
  return /\b[A-Za-z]{2,}\s+[A-Za-z]{2,}\b/.test(prose) || /^[A-Z]?[a-z]{2,}[.!?:…]*$/.test(prose.trim());
}

/** Accept only useful translated content, including older cached responses. */
export function cleanTranslation(value: unknown): string | null {
  if (typeof value !== "string") return null;
  let text = value.trim().replace(/^```(?:json|text)?\s*([\s\S]*?)\s*```$/i, "$1").trim();
  if (text.startsWith("{")) {
    try {
      const object = JSON.parse(text);
      const content = ["translation", "translated_text", "text", "译文", "翻译结果", "待翻译文本"].map(key => object[key]).find(item => typeof item === "string");
      if (typeof content !== "string") return null;
      text = content.trim();
    } catch { return null; }
  }
  text = text.replace(/^[\s*#>]*(?:待翻译文本|译文|翻译结果|中文译文|翻译后的文本|以下是(?:中文)?译文|翻译如下)[\s*]*[：:]\s*/, "").trim();
  if (text.startsWith('"')) {
    try { const unquoted = JSON.parse(text); if (typeof unquoted === "string") text = unquoted.trim(); } catch { /* Plain quoted prose is kept. */ }
  }
  if (/待翻译文本|翻译(?:暂时|暂)?不可用|中文转换(?:暂)?不可用|(?:无法|不能)(?:完成)?翻译|请(?:提供|输入).{0,12}(?:翻译|文本)|未(?:提供|收到).{0,10}文本/.test(text)) return null;
  return text && /[\u4e00-\u9fff]/.test(text) && !needsTranslation(text) ? text : null;
}

export function translationSource(text: string, streaming = false): string {
  const tail = text.trim().slice(-2400);
  if (!streaming) return tail;
  // Translate a completed sentence while the next one is still streaming.
  const sentences = tail.match(/[\s\S]*?[.!?。！？](?=\s|$)/g);
  return sentences?.length ? sentences.join("").trim() : tail;
}

/** Stable, append-only chunks: completed sentences/paragraphs, or bounded long prose.
 * Leave the open tail buffered until a boundary, idle flush, or stream end.
 */
export function translationSegments(text: string, flush = false): string[] {
  const segments: string[] = [];
  let remaining = text;
  while (remaining.trim()) {
    const boundaries = /\n+|[.!?。！？](?=\s)/g;
    let end = 0;
    let boundary: RegExpExecArray | null;
    while ((boundary = boundaries.exec(remaining.slice(0, 481)))) {
      if (boundary[0].includes("\n") || boundary.index + boundary[0].length >= 120) {
        end = boundary.index + boundary[0].length;
        break;
      }
    }
    if (!end || end > 480) {
      if (remaining.length > 480) {
        const whitespace = remaining.slice(0, 480).search(/\s+\S*$/);
        end = whitespace >= 200 ? whitespace + 1 : 480;
      } else if (flush) end = remaining.length;
      else break;
    }
    const source = remaining.slice(0, end).trim();
    if (source) segments.push(source);
    remaining = remaining.slice(end);
  }
  return segments;
}

export function translatedSegments(queue: TranslationQueue, id: string, segments: string[]): string {
  const output: string[] = [];
  for (let index = 0; index < segments.length; index++) {
    const source = segments[index];
    if (!needsTranslation(source) && /[\u4e00-\u9fff]/.test(source)) {
      const content = cleanTranslation(source);
      if (content) output.push(content);
    }
    else {
      const translated = queue.get(`${id}:segment:${index}`, source);
      if (translated) output.push(translated);
      else if (queue.unavailable(source)) continue; // Failed paragraphs never become fake reasoning.
      else break; // Never show paragraph 2 ahead of paragraph 1.
    }
  }
  return output.join("\n");
}

export function streamingTranslationSegments(text: string, active: boolean, settledText: string): string[] {
  const segments = translationSegments(text, !active);
  // An idle-flushed tail stays visible when generation resumes. Hold that
  // immutable tail until the next real boundary, instead of retracting it.
  if (active && settledText && text.startsWith(settledText)) {
    const settled = translationSegments(settledText, true);
    if (settled.length > segments.length) segments.push(...settled.slice(segments.length));
  }
  return segments;
}

type Row = { source: string; notify: () => void; priority: number; translated?: { source: string; text: string }; attempted?: string };
type Translate = (texts: string[], signal: AbortSignal) => Promise<(string | null)[]>;

async function requestTranslations(texts: string[], signal: AbortSignal): Promise<(string | null)[]> {
  const response = await fetch("/api/process-studio/translate", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ texts }), signal,
  });
  if (!response.ok) throw new Error("Translation unavailable");
  const data = await response.json();
  if (!Array.isArray(data.translations) || data.translations.length !== texts.length) throw new Error("Invalid translation");
  return data.translations.map(cleanTranslation);
}

export class TranslationQueue {
  private rows = new Map<string, Row>();
  private cache = new Map<string, { text: string | null; expires: number }>();
  private timer?: ReturnType<typeof setTimeout>;
  private activeBatches = 0;
  private inflightSources = new Set<string>();
  private nextRun = 0;
  constructor(private translate: Translate = requestTranslations, private interval = 1200, private batchSize = 6, private delay = 300, private maxConcurrent = 1) {}

  subscribe(id: string, text: string, notify: () => void, priority = 0): () => void {
    const source = translationSource(text);
    const previous = this.rows.get(id)?.translated;
    const row: Row = { source, notify, priority, translated: previous && source.startsWith(previous.source) ? previous : undefined };
    this.rows.set(id, row);
    this.schedule();
    return () => {
      // Keep progressive translations across subscription changes, without
      // retaining unmounted history rows or their React callbacks.
      if (this.rows.get(id) === row) {
        row.notify = () => {};
        queueMicrotask(() => { if (this.rows.get(id) === row) this.rows.delete(id); });
      }
    };
  }

  get(id: string, text: string): string | undefined {
    const source = translationSource(text);
    const cached = this.cache.get(source);
    if (cached && cached.expires > Date.now() && cached.text) return cleanTranslation(cached.text) || undefined;
    const translated = this.rows.get(id)?.translated;
    return translated && source.startsWith(translated.source) ? cleanTranslation(translated.text) || undefined : undefined;
  }

  unavailable(text: string): boolean {
    const cached = this.cache.get(translationSource(text));
    // Failure stays skippable until a successful retry, so later paragraphs
    // don't disappear when the retry cooldown expires.
    return !!cached && cleanTranslation(cached.text) === null;
  }

  private schedule() {
    if (this.activeBatches < this.maxConcurrent && !this.timer) this.timer = setTimeout(() => { this.timer = undefined; void this.run(); }, Math.max(this.delay, this.nextRun - Date.now()));
  }

  private async run() {
    const pending = [...this.rows.entries()].reverse().sort((a, b) => b[1].priority - a[1].priority).filter(([, row]) => {
      const cached = this.cache.get(row.source);
      const required = needsTranslation(row.source) || (!/[\u4e00-\u9fff]/.test(row.source) && /[A-Za-z]/.test(row.source));
      return required && row.attempted !== row.source && !this.inflightSources.has(row.source) && !(cached && cached.expires > Date.now());
    }).slice(0, this.batchSize);
    if (!pending.length) return;
    this.activeBatches++;
    const sources = [...new Set(pending.map(([, row]) => row.source))];
    pending.forEach(([, row]) => { row.attempted = row.source; });
    sources.forEach(source => this.inflightSources.add(source));
    this.schedule();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 9000);
    try {
      const values = await this.translate(sources, controller.signal);
      sources.forEach((source, index) => {
        const text = cleanTranslation(values[index]);
        this.cache.delete(source);
        this.cache.set(source, { text, expires: Date.now() + (text ? 3600000 : 20000) });
        for (const row of this.rows.values()) {
          if (text && row.source.startsWith(source)) row.translated = { source, text };
          if (row.source.startsWith(source)) row.notify();
        }
      });
    } catch {
      sources.forEach(source => this.cache.set(source, { text: null, expires: Date.now() + 20000 }));
      for (const row of this.rows.values()) if (sources.includes(row.source)) row.notify();
    } finally {
      clearTimeout(timeout);
      while (this.cache.size > 512) this.cache.delete(this.cache.keys().next().value!);
      sources.forEach(source => this.inflightSources.delete(source));
      this.activeBatches--;
      this.nextRun = Date.now() + this.interval;
      this.schedule();
    }
  }
}
