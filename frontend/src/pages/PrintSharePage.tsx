/**
 * Guest Print Share page (#3461).
 *
 * `/share/{publicId}` — no Bambuddy login. Shows live camera + progress while
 * the share is active, then an end-cap with the project link when the print ends.
 */
import { useEffect, useMemo } from 'react';
import { useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ExternalLink, Layers, Clock, Timer } from 'lucide-react';
import { api } from '../api/client';
import { formatDuration, formatETA, type TimeFormat } from '../utils/date';
import { useOverlayCameraRecovery } from '../hooks/useOverlayCameraRecovery';

export function PrintSharePage() {
  const { publicId = '' } = useParams<{ publicId: string }>();
  const { t } = useTranslation();

  const { data: share, isError, error } = useQuery({
    queryKey: ['printSharePublic', publicId],
    queryFn: () => api.getPrintSharePublic(publicId),
    enabled: Boolean(publicId),
    refetchInterval: (query) => (query.state.data?.status === 'active' ? 2000 : false),
  });

  const active = share?.status === 'active';
  const printerId = share?.printer_id ?? 0;
  const token = publicId;

  const { data: overlay } = useQuery({
    queryKey: ['overlayStatus', printerId, token],
    queryFn: () => api.getOverlayStatus(printerId, token),
    enabled: active && printerId > 0,
    refetchInterval: 2000,
  });

  const status = active ? overlay ?? share : share;
  const { imageKey, handleStreamError } = useOverlayCameraRecovery(active && printerId > 0);
  const timeFormat: TimeFormat = (status?.time_format as TimeFormat) || 'system';

  useEffect(() => {
    const title = share?.title || share?.printer_name || t('printShare.title', 'Print Share');
    document.title = title;
    // Discourage search indexing of share URLs.
    let robots = document.querySelector('meta[name="robots"]');
    if (!robots) {
      robots = document.createElement('meta');
      robots.setAttribute('name', 'robots');
      document.head.appendChild(robots);
    }
    robots.setAttribute('content', 'noindex, nofollow');
    return () => {
      document.title = 'Bambuddy';
    };
  }, [share, t]);

  const streamUrl = useMemo(() => {
    if (!active || !printerId) return null;
    return `/api/v1/printers/${printerId}/camera/stream?fps=12&t=${imageKey}&token=${encodeURIComponent(token)}`;
  }, [active, printerId, imageKey, token]);

  if (!publicId) {
    return (
      <div className="min-h-screen bg-zinc-950 flex items-center justify-center text-white">
        {t('printShare.invalid', 'Invalid share link')}
      </div>
    );
  }

  if (isError) {
    const statusCode = (error as { status?: number })?.status;
    return (
      <div className="min-h-screen bg-zinc-950 flex items-center justify-center text-white px-6 text-center">
        {statusCode === 404
          ? t('printShare.notFound', 'This share link was not found or has been revoked.')
          : t('printShare.loadError', 'Could not load this share link.')}
      </div>
    );
  }

  if (!share) {
    return (
      <div className="min-h-screen bg-zinc-950 flex items-center justify-center text-zinc-400">
        {t('common.loading', 'Loading…')}
      </div>
    );
  }

  const progress = status?.progress ?? 0;
  const isPrinting = status?.state === 'RUNNING' || status?.state === 'PAUSE';
  const remaining = status?.remaining_time;

  return (
    <div className="min-h-screen bg-zinc-950 text-white relative overflow-hidden">
      {active && streamUrl ? (
        <img
          key={imageKey}
          src={streamUrl}
          alt={t('printShare.camera', 'Print camera')}
          className="absolute inset-0 w-full h-full object-contain"
          style={share.camera_rotation ? { transform: `rotate(${share.camera_rotation}deg)` } : undefined}
          onError={handleStreamError}
        />
      ) : (
        <div className="absolute inset-0 flex items-center justify-center bg-zinc-900">
          <div className="text-center px-6 max-w-md">
            <p className="text-2xl font-semibold mb-2">
              {share.end_reason === 'failed'
                ? t('printShare.endedFailed', 'Print failed')
                : share.end_reason === 'cancelled'
                  ? t('printShare.endedCancelled', 'Print cancelled')
                  : share.end_reason === 'revoked'
                    ? t('printShare.endedRevoked', 'Share revoked')
                    : t('printShare.endedCompleted', 'Print finished')}
            </p>
            <p className="text-zinc-400 text-sm">
              {t('printShare.endedHint', 'Live video for this share has ended.')}
            </p>
          </div>
        </div>
      )}

      <div className="absolute bottom-0 left-0 right-0 z-10 bg-gradient-to-t from-black/85 via-black/55 to-transparent p-6 pt-16">
        <div className="max-w-3xl">
          {share.printer_name && (
            <div className="text-sm text-white/70 mb-1">{share.printer_name}</div>
          )}
          <div className="text-xl font-semibold truncate mb-2">
            {share.title || status?.current_print || t('printShare.untitled', 'Print')}
          </div>

          {active && isPrinting && (
            <>
              <div className="flex items-center justify-between text-sm mb-1">
                <span className="text-white/70">{t('streamOverlay.progress', 'Progress')}</span>
                <span className="font-bold">{Math.round(progress)}%</span>
              </div>
              <div className="w-full bg-white/20 rounded-full h-2 mb-3">
                <div
                  className="bg-emerald-500 h-2 rounded-full transition-all duration-500"
                  style={{ width: `${Math.min(100, Math.max(0, progress))}%` }}
                />
              </div>
              <div className="flex flex-wrap gap-4 text-sm text-white/80 mb-3">
                {status?.layer_num != null && status?.total_layers != null && status.total_layers > 0 && (
                  <span className="inline-flex items-center gap-1.5">
                    <Layers className="w-4 h-4" />
                    {status.layer_num} / {status.total_layers}
                  </span>
                )}
                {remaining != null && remaining > 0 && (
                  <>
                    <span className="inline-flex items-center gap-1.5">
                      <Timer className="w-4 h-4" />
                      {formatDuration(remaining * 60)}
                    </span>
                    <span className="inline-flex items-center gap-1.5">
                      <Clock className="w-4 h-4" />
                      {formatETA(remaining, timeFormat, t)}
                    </span>
                  </>
                )}
              </div>
            </>
          )}

          {share.project_url && (
            <a
              href={share.project_url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 text-emerald-400 hover:text-emerald-300 text-sm font-medium"
            >
              <ExternalLink className="w-4 h-4" />
              {t('printShare.projectLink', 'View project / model')}
            </a>
          )}
        </div>
      </div>
    </div>
  );
}
