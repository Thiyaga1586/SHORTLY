import type { CacheStats } from "../types";
import { SectionTitle } from "./RingPanel";

interface CachePanelProps {
  cache: CacheStats | null;
}

export function CachePanel({ cache }: CachePanelProps) {
  const hitRatePct = cache ? cache.hit_rate * 100 : 0;
  const hitRateGood = hitRatePct > 50;

  return (
    <section className="bg-panel border border-border rounded-xl p-5 sm:p-6">
      <SectionTitle title="Cache" subtitle="Shared Redis layer" />

      {!cache ? (
        <div className="h-24 bg-white/5 rounded-lg animate-pulse" />
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm font-mono text-white/60 mb-5">
            <span>
              <span className="text-white/40">Capacity:</span>{" "}
              <span className="text-white/90 font-semibold">
                {cache.capacity}
              </span>
            </span>
            <span>
              <span className="text-white/40">Live entries:</span>{" "}
              <span className="text-white/90 font-semibold">
                {cache.size}
              </span>
            </span>
            <span
              className={`px-2 py-0.5 rounded border text-xs cursor-help ${
                hitRateGood
                  ? "bg-ok/10 text-ok border-ok/30"
                  : "bg-warn/10 text-warn border-warn/30"
              }`}
              title="Low hit rate is expected with light demo traffic — each unique URL is typically only requested once or twice, leaving little to cache yet."
            >
              Hit rate: {hitRatePct.toFixed(1)}%
            </span>
          </div>

          <div className="grid grid-cols-3 gap-3">
            <MetricBox label="Hits" value={cache.hits} accent="text-ok" />
            <MetricBox
              label="Misses"
              value={cache.misses}
              accent="text-warn"
            />
            <MetricBox
              label="Evictions"
              value={cache.evictions}
              accent="text-white/70"
            />
          </div>
        </>
      )}
    </section>
  );
}

function MetricBox({
  label,
  value,
  accent,
}: {
  label: string;
  value: number;
  accent: string;
}) {
  return (
    <div className="bg-panel2 border border-border rounded-lg px-3 py-3 text-center">
      <div className={`text-xl font-bold font-mono ${accent}`}>{value}</div>
      <div className="text-xs text-white/40 mt-0.5">{label}</div>
    </div>
  );
}
