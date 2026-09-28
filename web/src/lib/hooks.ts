"use client";

import { useQuery, useMutation, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { ApiError } from "@/lib/auth-client";

export function useVideos() {
  return useQuery({
    queryKey: ["videos"],
    queryFn: () => api.getVideos(),
  });
}

export function useVideo(id: string) {
  return useQuery({
    queryKey: ["video", id],
    queryFn: () => api.getVideo(id),
    enabled: !!id,
  });
}

export function useChannels() {
  return useQuery({
    queryKey: ["channels"],
    queryFn: () => api.getChannels(),
  });
}

export function useChannel(id: string) {
  return useQuery({
    queryKey: ["channel", id],
    queryFn: () => api.getChannel(id),
    enabled: !!id,
  });
}

export function useTickers() {
  return useQuery({
    queryKey: ["tickers"],
    queryFn: () => api.getTickers(),
  });
}

export function useTopETFs() {
  return useQuery({
    queryKey: ["topETFs"],
    queryFn: () => api.getTopETFs(),
  });
}

export function useTicker(ticker: string) {
  return useQuery({
    queryKey: ["ticker", ticker],
    queryFn: () => api.getTicker(ticker),
    enabled: !!ticker,
  });
}

export function useTickerSentiment(ticker: string, days = 30) {
  return useQuery({
    queryKey: ["tickerSentiment", ticker, days],
    queryFn: () => api.getTickerSentimentTimeline(ticker, days),
    enabled: !!ticker,
    placeholderData: keepPreviousData,
  });
}

export function useTickerPriceHistory(ticker: string, days = 30) {
  return useQuery({
    queryKey: ["tickerPriceHistory", ticker, days],
    queryFn: () => api.getTickerPriceHistory(ticker, days),
    enabled: !!ticker,
    placeholderData: keepPreviousData,
  });
}

export function useThemes() {
  return useQuery({
    queryKey: ["themes"],
    queryFn: () => api.getThemes(),
  });
}

export function useTheme(id: string) {
  return useQuery({
    queryKey: ["theme", id],
    queryFn: () => api.getTheme(id),
    enabled: !!id,
  });
}

export function useActivity(opts?: { limit?: number; unreadOnly?: boolean }) {
  return useQuery({
    queryKey: ["activity", opts?.limit, opts?.unreadOnly],
    queryFn: () => api.getActivity(opts),
  });
}

export function useUnreadCount() {
  return useQuery({
    queryKey: ["unreadCount"],
    queryFn: () => api.getActivityUnreadCount(),
    refetchInterval: 30_000,
  });
}

export function useDashboardData() {
  const query = useQuery({
    queryKey: ["dashboardSummary"],
    queryFn: () => api.getDashboardSummary(),
    staleTime: 5 * 60_000, // taxonomy barely changes - keep 5 min
    retry: false, // don't retry on 401 (avoids 2× cold-load time)
  });

  return {
    isLoading: query.isLoading,
    isError: query.isError,
    data: query.data
      ? {
        total_videos: query.data.total_videos ?? (query.data.videos?.length || 0),
        videos: query.data.videos || [],
        channels: query.data.channels || [],
        themes: [], // full themes no longer fetched; use theme_counts
        tickers: query.data.tickers || [],
        etfs: query.data.etfs || [],
        theme_counts: query.data.theme_counts || { sectors: 0, industries: 0, themes: 0, narratives: 0 },
      }
      : {
        total_videos: 0,
        videos: [],
        channels: [],
        themes: [],
        tickers: [],
        etfs: [],
        theme_counts: { sectors: 0, industries: 0, themes: 0, narratives: 0 },
      },
    refetch: query.refetch,
  };
}

export function useSearch(
  query: string,
  type: "keyword" | "semantic" | "hybrid" = "hybrid",
  sort: "relevance" | "recent" = "relevance",
  limit = 20,
) {
  return useQuery({
    queryKey: ["search", query, type, sort, limit],
    queryFn: () => api.search(query, type, sort, limit),
    enabled: !!query.trim(),
    placeholderData: keepPreviousData,
  });
}

export function useSearchAnswer(query: string, segmentIds: string[]) {
  // Key on the joined ids (stable identity) so refetches only happen when
  // the underlying result set actually changes.
  const joined = segmentIds.join(",");
  return useQuery({
    queryKey: ["searchAnswer", query, joined],
    queryFn: () => api.searchAnswer(query, segmentIds),
    enabled: !!query.trim() && segmentIds.length >= 3,
    staleTime: 10 * 60_000,
    retry: false,
  });
}

export function useSearchCoverage(query: string, segmentIds: string[]) {
  const joined = segmentIds.join(",");
  return useQuery({
    queryKey: ["searchCoverage", query, joined],
    queryFn: () => api.searchCoverage(query, segmentIds),
    enabled: !!query.trim() && segmentIds.length > 0,
    staleTime: 10 * 60_000,
    retry: false,
  });
}

export function useTickerFlowDashboard(periodDays = 7) {
  return useQuery({
    queryKey: ["tickerflowDashboard", periodDays],
    queryFn: () => api.getTickerFlowDashboard(periodDays),
  });
}

export function useBackfillChannel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ youtubeChannelId, maxVideos }: { youtubeChannelId: string; maxVideos?: number }) =>
      api.backfillChannel(youtubeChannelId, maxVideos),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["channels"] });
    },
  });
}

export function useIngestVideo() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ channelDbId, youtubeVideoId }: { channelDbId: string; youtubeVideoId: string }) =>
      api.ingestSingleVideo(channelDbId, youtubeVideoId),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ["channel", variables.channelDbId] });
      queryClient.invalidateQueries({ queryKey: ["videos"] });
    },
  });
}

/* ── Auth & usage hooks ─────────────────────────────────────────── */

export function useMe() {
  const query = useQuery({
    queryKey: ["me"],
    queryFn: () => api.getMe(),
    retry: false,
    staleTime: 60_000,
  });

  // Invite-only beta is retired: open signup, so never gate on invite status.
  // `inviteRequired` is kept (always false) for backward-compat with callers.
  const error = query.error;
  const inviteRequired = false;
  const unauthorized =
    error instanceof ApiError &&
    (error.status === 401 || error.code === "unauthorized" || error.status === 0);

  return { ...query, user: query.data, isAdmin: query.data?.role === "admin", inviteRequired, unauthorized };
}

export function useRedeemInvite() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (code: string) => api.redeemInvite(code),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  });
}

export function useMyUsage(days = 30) {
  return useQuery({
    queryKey: ["myUsage", days],
    queryFn: () => api.getMyUsage(days),
  });
}
