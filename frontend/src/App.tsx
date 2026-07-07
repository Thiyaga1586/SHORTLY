import { Header } from "./components/Header";
import { RingPanel } from "./components/RingPanel";
import { CachePanel } from "./components/CachePanel";
import { ShortenPanel } from "./components/ShortenPanel";
import { useStats } from "./useStats";

export default function App() {
  const { stats, error } = useStats(5000);

  return (
    <div className="min-h-screen bg-bg text-white font-sans">
      <div className="max-w-3xl mx-auto px-4 sm:px-6 py-10 sm:py-14">
        <Header
          redisOk={stats?.redis_ok ?? null}
          nodeCount={stats?.ring.total_nodes ?? null}
        />

        {error && (
          <div className="mb-6 text-sm text-danger bg-danger/5 border border-danger/20 rounded-lg px-3 py-2 font-mono">
            Couldn't reach the backend: {error}
          </div>
        )}

        <div className="space-y-6">
          <RingPanel ring={stats?.ring ?? null} />
          <CachePanel cache={stats?.cache ?? null} />
          <ShortenPanel />
        </div>

        <footer className="mt-10 text-xs text-white/30 font-mono text-center">
          Auto-refreshes every 5s
        </footer>
      </div>
    </div>
  );
}
