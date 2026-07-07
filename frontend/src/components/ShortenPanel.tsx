import { useState } from "react";
import { shortenUrl } from "../api";
import { SectionTitle } from "./RingPanel";

export function ShortenPanel() {
  const [url, setUrl] = useState("");
  const [days, setDays] = useState(7);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  async function submit(targetUrl: string, targetDays: number) {
    setBusy(true);
    setError(null);
    setResult(null);
    setCopied(false);
    try {
      const data = await shortenUrl(targetUrl, targetDays);
      setResult(data.short_url);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!url.trim()) return;
    submit(url.trim(), days);
  }

  function tryExample() {
    setUrl("https://github.com/");
    submit("https://github.com/", 7);
  }

  async function copyResult() {
    if (!result) return;
    await navigator.clipboard.writeText(result);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <section className="bg-panel border border-border rounded-xl p-5 sm:p-6">
      <SectionTitle title="Shorten a URL" subtitle="Try it live" />

      <form
        onSubmit={handleSubmit}
        className="flex flex-col sm:flex-row gap-3"
      >
        <input
          type="text"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://example.com"
          className="flex-1 bg-panel2 border border-border rounded-lg px-3 py-2 text-sm text-white/90 placeholder:text-white/30 focus:outline-none focus:ring-2 focus:ring-accent2/50"
        />
        <input
          type="number"
          min={1}
          value={days}
          onChange={(e) => setDays(parseInt(e.target.value) || 1)}
          className="w-full sm:w-20 bg-panel2 border border-border rounded-lg px-3 py-2 text-sm text-white/90 focus:outline-none focus:ring-2 focus:ring-accent2/50"
        />
        <button
          type="submit"
          disabled={busy}
          className="px-5 py-2 rounded-lg bg-accent2 hover:bg-accent2/80 text-white font-medium text-sm transition-colors disabled:opacity-50 whitespace-nowrap"
        >
          {busy ? "Shortening…" : "Shorten"}
        </button>
        <button
          type="button"
          onClick={tryExample}
          disabled={busy}
          className="px-5 py-2 rounded-lg bg-white/5 hover:bg-white/10 border border-border text-white/70 font-medium text-sm transition-colors disabled:opacity-50 whitespace-nowrap"
          title="Fill in a sample URL and shorten it"
        >
          Try an example
        </button>
      </form>

      {result && (
        <div className="mt-4 flex items-center gap-2 text-sm bg-ok/5 border border-ok/20 rounded-lg px-3 py-2">
          <span className="text-white/50">Short URL:</span>
          <a
            href={result}
            target="_blank"
            rel="noreferrer"
            className="text-accent hover:underline font-mono truncate"
          >
            {result}
          </a>
          <button
            onClick={copyResult}
            className="ml-auto px-2 py-1 rounded bg-white/5 hover:bg-white/10 text-white/60 text-xs shrink-0"
          >
            {copied ? "Copied!" : "Copy"}
          </button>
        </div>
      )}

      {error && (
        <div className="mt-4 text-sm text-danger bg-danger/5 border border-danger/20 rounded-lg px-3 py-2">
          {error}
        </div>
      )}
    </section>
  );
}
