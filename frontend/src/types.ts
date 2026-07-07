export interface RingStats {
  total_nodes: number;
  total_vnodes: number;
  load_std_dev: number;
  vnode_distribution: Record<string, number>;
}

export interface CacheStats {
  capacity: number;
  size: number;
  hits: number;
  misses: number;
  evictions: number;
  hit_rate: number;
}

export interface StatsResponse {
  redis_ok: boolean;
  ring: RingStats;
  cache: CacheStats;
}

export interface ShortenResponse {
  short_url: string;
  org_url?: string;
  error?: string;
}
