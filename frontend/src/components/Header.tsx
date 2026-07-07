interface HeaderProps {
  redisOk: boolean | null;
  nodeCount: number | null;
}

export function Header({ redisOk, nodeCount }: HeaderProps) {
  return (
    <header className="mb-10">
      <div className="flex items-center gap-3">
        <span className="text-3xl">⚡</span>
        <h1 className="text-2xl sm:text-3xl font-bold text-white tracking-tight">
          Shortly
        </h1>
        <span className="text-sm text-white/40 font-mono hidden sm:inline">
          / distributed URL shortener
        </span>
      </div>

      <p className="mt-3 max-w-2xl text-sm sm:text-base text-white/60 leading-relaxed">
        A horizontally-scaled URL shortener using{" "}
        <span className="text-accent">consistent hashing</span> across
        independent Flask nodes, backed by PostgreSQL and a shared Redis
        cache.
      </p>

      <div className="mt-3 flex flex-wrap gap-2 text-xs font-mono text-white/40">
        {["Flask", "Gunicorn", "PostgreSQL", "Redis", "Docker Compose", "Cloudflare Tunnel"].map(
          (tech) => (
            <span
              key={tech}
              className="px-2 py-1 rounded-md bg-white/5 border border-white/10"
            >
              {tech}
            </span>
          )
        )}
      </div>

      <div className="mt-5 flex items-center gap-4 text-sm">
        <StatusBadge
          label="Redis"
          ok={redisOk}
          okText="connected"
          badText="down"
        />
        <span className="text-white/30">·</span>
        <span className="text-white/50 font-mono">
          Nodes:{" "}
          <span className="text-white/80 font-semibold">
            {nodeCount ?? "—"}
          </span>
        </span>
      </div>
    </header>
  );
}

function StatusBadge({
  label,
  ok,
  okText,
  badText,
}: {
  label: string;
  ok: boolean | null;
  okText: string;
  badText: string;
}) {
  const state = ok === null ? "unknown" : ok ? "ok" : "bad";
  const styles = {
    ok: "bg-ok/10 text-ok border-ok/30",
    bad: "bg-danger/10 text-danger border-danger/30",
    unknown: "bg-white/5 text-white/40 border-white/10",
  }[state];

  return (
    <span className="flex items-center gap-1.5 font-mono text-white/50">
      {label}:
      <span className={`px-2 py-0.5 rounded border text-xs ${styles}`}>
        {ok === null ? "…" : ok ? okText : badText}
      </span>
    </span>
  );
}
