import { useEffect, useState, useRef } from "react";
import { fetchStats } from "./api";
import type { StatsResponse } from "./types";

interface UseStatsResult {
  stats: StatsResponse | null;
  error: string | null;
  loading: boolean;
}

/** Polls /api/stats on an interval, pausing while the tab is hidden. */
export function useStats(intervalMs = 5000): UseStatsResult {
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      try {
        const data = await fetchStats();
        if (!cancelled) {
          setStats(data);
          setError(null);
          setLoading(false);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Unknown error");
          setLoading(false);
        }
      }
    }

    function schedule() {
      if (document.hidden) return;
      poll();
    }

    schedule();
    timerRef.current = window.setInterval(schedule, intervalMs);

    const onVisibility = () => {
      if (!document.hidden) poll();
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      cancelled = true;
      if (timerRef.current) window.clearInterval(timerRef.current);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [intervalMs]);

  return { stats, error, loading };
}
