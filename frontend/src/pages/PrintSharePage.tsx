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
import { ExternalLink, Layers, Clock, Timer, Thermometer } from 'lucide-react';
import { api } from '../api/client';
import { formatDuration, formatETA, type TimeFormat } from '../utils/date';
import { useOverlayCameraRecovery } from '../hooks/useOverlayCameraRecovery';

function formatHexColor(raw: string | null | undefined): string {
  if (!raw) return '#71717a';
  const clean = raw.replace(/^#/, '').slice(0, 6);
  return clean.length === 6 ? `#${clean}` : '#71717a';
}

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
    const livePrintName = status?.current_print || status?.gcode_file;
    const title =
      (!share?.end_with_print && livePrintName) ||
      share?.title ||
      share?.printer_name ||
      t('printShare.title', 'Print Share');
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
  }, [share, status, t]);

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
  const isPrinting =
    status?.state === 'RUNNING' ||
    status?.state === 'PAUSE' ||
    status?.state === 'PREPARE' ||
    status?.state === 'SLICING';
  const remaining = status?.remaining_time;
  const temps = status?.temperatures ?? share.temperatures ?? {};
  const amsTrays = share.ams_trays ?? [];
  const awaitingPlateClear = Boolean(share.awaiting_plate_clear) && !isPrinting;

  // For permanent links (end_with_print === false), automatically show whatever print starts next.
  const liveJobTitle = (status?.current_print || status?.gcode_file || '').toString().split('/').pop() || '';
  const displayHeadline = isPrinting
    ? (!share.end_with_print && liveJobTitle ? liveJobTitle : share.title || liveJobTitle || t('printShare.untitled', 'Print'))
    : awaitingPlateClear
      ? t('printers.plateStatus.needsClear', 'Print finished — plate needs clearing')
      : share.title || t('printers.status.idle', 'Printer Idle & Ready');

  const stateBadge = !status?.connected
    ? { label: t('printers.connection.offline', 'Offline'), cls: 'bg-red-500/85 text-white' }
    : status?.state === 'PAUSE'
      ? { label: t('printers.status.paused', 'Paused'), cls: 'bg-amber-500/85 text-black' }
      : isPrinting
        ? { label: status?.stg_cur_name || t('printers.status.printing', 'Printing'), cls: 'bg-emerald-500/85 text-black' }
        : awaitingPlateClear
          ? { label: t('printers.plateStatus.awaitingClear', 'Awaiting Plate Clear'), cls: 'bg-amber-500/85 text-black' }
          : { label: t('printers.status.idle', 'Idle'), cls: 'bg-zinc-700/85 text-zinc-200' };

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

      {active && (
        <div className="absolute top-4 left-4 z-10 flex items-center gap-2">
          <span className={`px-2.5 py-1 rounded-full text-xs font-semibold uppercase tracking-wider shadow ${stateBadge.cls}`}>
            {stateBadge.label}
          </span>
          {!share.end_with_print && (
            <span className="px-2.5 py-1 rounded-full text-xs font-medium bg-black/60 text-zinc-300 border border-white/10">
              {t('printShare.permanentBadge', 'Live Printer Status')}
            </span>
          )}
        </div>
      )}

      <div className="absolute bottom-0 left-0 right-0 z-10 bg-gradient-to-t from-black/90 via-black/65 to-transparent p-6 pt-16">
        <div className="max-w-3xl space-y-3">
          <div>
            {share.printer_name && (
              <div className="text-sm text-white/70 mb-0.5">
                {share.printer_name}
                {share.model ? ` • ${share.model}` : ''}
              </div>
            )}
            <div className="text-xl font-semibold truncate">{displayHeadline}</div>
          </div>

          {active && isPrinting && (
            <div>
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
              <div className="flex flex-wrap gap-4 text-sm text-white/80">
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
            </div>
          )}

          {active && (Object.keys(temps).length > 0 || amsTrays.length > 0) && (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-white/80 pt-1">
              {temps.nozzle != null && (
                <span className="inline-flex items-center gap-1 bg-white/10 px-2 py-1 rounded-md">
                  <Thermometer className="w-3.5 h-3.5 text-orange-400" />
                  {t('printers.temperatures.nozzle', 'Nozzle')}: {Math.round(temps.nozzle)}°C
                </span>
              )}
              {temps.bed != null && (
                <span className="inline-flex items-center gap-1 bg-white/10 px-2 py-1 rounded-md">
                  <Thermometer className="w-3.5 h-3.5 text-sky-400" />
                  {t('printers.temperatures.bed', 'Bed')}: {Math.round(temps.bed)}°C
                </span>
              )}
              {temps.chamber != null && (
                <span className="inline-flex items-center gap-1 bg-white/10 px-2 py-1 rounded-md">
                  <Thermometer className="w-3.5 h-3.5 text-emerald-400" />
                  {t('printers.temperatures.chamber', 'Chamber')}: {Math.round(temps.chamber)}°C
                </span>
              )}
              {amsTrays.length > 0 && (
                <div className="inline-flex flex-wrap items-center gap-1.5">
                  {amsTrays.map((tray) => (
                    <span
                      key={`${tray.ams_id}-${tray.slot_id}`}
                      className="inline-flex items-center gap-1.5 bg-white/10 px-2 py-1 rounded-md"
                      title={
                        tray.remain != null
                          ? `${tray.type} (${tray.remain}%)`
                          : tray.type
                      }
                    >
                      <span
                        className="w-2.5 h-2.5 rounded-full border border-white/30 flex-shrink-0"
                        style={{ backgroundColor: formatHexColor(tray.color) }}
                      />
                      <span className="font-medium text-white">{tray.type}</span>
                      {tray.remain != null && (
                        <span className="text-white/60">{tray.remain}%</span>
                      )}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}

          {share.project_url && (
            <div>
              <a
                href={share.project_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-2 text-emerald-400 hover:text-emerald-300 text-sm font-medium"
              >
                <ExternalLink className="w-4 h-4" />
                {t('printShare.projectLink', 'View project / model')}
              </a>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
