import type { StatsResponse, ShortenResponse } from "./types";

export async function fetchStats(): Promise<StatsResponse> {
  const res = await fetch("/api/stats");
  if (!res.ok) throw new Error(`Stats request failed: ${res.status}`);
  return res.json();
}

export async function shortenUrl(
  orgUrl: string,
  expiryDays: number
): Promise<ShortenResponse> {
  const res = await fetch("/shorten", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ org_url: orgUrl, expiry_days: expiryDays }),
  });
  const data: ShortenResponse = await res.json();
  if (!res.ok) {
    throw new Error(data.error || `Request failed: ${res.status}`);
  }
  return data;
}
