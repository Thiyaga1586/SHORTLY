import type { RingStats } from "../types";

interface RingPanelProps {
  ring: RingStats | null;
}

export function RingPanel({ ring }: RingPanelProps) {
  return (
    <section className="bg-panel border border-border rounded-xl p-5 sm:p-6">
      <SectionTitle title="Ring" subtitle="Consistent hashing distribution" />

      {!ring ? (
        <SkeletonRows count={3} />
      ) : (
        <>
          <div className="flex flex-wrap gap-x-8 gap-y-2 text-sm font-mono text-white/60 mb-5">
            <Stat label="Nodes" value={ring.total_nodes} />
            <Stat label="Virtual nodes" value={ring.total_vnodes} />
            <Stat label="Load std-dev" value={ring.load_std_dev.toFixed(2)} />
          </div>

          <div className="space-y-3">
            {Object.entries(ring.vnode_distribution).map(([nodeId, count]) => {
              const pct = ring.total_vnodes
                ? (100 * count) / ring.total_vnodes
                : 0;
              return (
                <div key={nodeId} className="group">
                  <div className="flex items-center justify-between text-sm mb-1">
                    <span className="font-mono text-white/80">{nodeId}</span>
                    <span className="font-mono text-white/40 text-xs">
                      {count} v-nodes · {pct.toFixed(1)}%
                    </span>
                  </div>
                  <div className="h-2 rounded-full bg-white/5 overflow-hidden">
                    <div
                      className="h-full rounded-full bg-gradient-to-r from-accent2 to-accent transition-all duration-500"
                      style={{ width: `${Math.min(pct, 100)}%` }}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        </>
      )}
    </section>
  );
}

export function SectionTitle({
  title,
  subtitle,
}: {
  title: string;
  subtitle?: string;
}) {
  return (
    <div className="mb-4">
      <h2 className="text-white/90 font-semibold text-lg">{title}</h2>
      {subtitle && (
        <p className="text-white/40 text-xs mt-0.5 font-mono">{subtitle}</p>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <span>
      <span className="text-white/40">{label}:</span>{" "}
      <span className="text-white/90 font-semibold">{value}</span>
    </span>
  );
}

function SkeletonRows({ count }: { count: number }) {
  return (
    <div className="space-y-3 animate-pulse">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="h-8 bg-white/5 rounded-lg" />
      ))}
    </div>
  );
}
