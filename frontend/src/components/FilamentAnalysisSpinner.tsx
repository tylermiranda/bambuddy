import { Loader2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { api, type SliceJobProgress } from '../api/client';
import { useToast } from '../contexts/ToastContext';

function formatElapsed(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const remS = s % 60;
  if (m < 60) return `${m}m ${remS}s`;
  const h = Math.floor(m / 60);
  const remM = m % 60;
  return `${h}h ${remM}m`;
}

export function FilamentAnalysisSpinner({
  requestId,
  sourceName,
}: {
  requestId: string;
  sourceName: string;
}) {
  const { t } = useTranslation();
  const { showPersistentToast, dismissToast } = useToast();
  const [elapsed, setElapsed] = useState(0);
  const [progress, setProgress] = useState<SliceJobProgress | null>(null);
  let prettyName = sourceName;
  try {
    prettyName = decodeURIComponent(sourceName);
  } catch {
    /* keep raw on malformed encoding */
  }

  useEffect(() => {
    const startedAt = Date.now();
    const id = setInterval(() => setElapsed(Math.floor((Date.now() - startedAt) / 1000)), 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    let cancelled = false;
    const id = setInterval(async () => {
      if (cancelled) return;
      const snap = await api.getPreviewSliceProgress(requestId);
      if (!cancelled && snap) setProgress(snap);
    }, 1000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [requestId]);

  const toastId = `slice-preview-${requestId}`;
  useEffect(() => {
    const hasUseful = progress && progress.stage && progress.total_percent > 0;
    const elapsedStr = formatElapsed(elapsed);
    if (hasUseful) {
      showPersistentToast(
        toastId,
        t(
          'slice.previewWithProgress',
          'Analyzing {{name}} — {{stage}} ({{percent}}%) — {{elapsed}}',
          {
            name: prettyName,
            stage: progress!.stage,
            percent: Math.min(100, Math.max(0, Math.round(progress!.total_percent))),
            elapsed: elapsedStr,
          },
        ),
        'loading',
      );
    } else {
      showPersistentToast(
        toastId,
        t('slice.previewToast', {
          name: prettyName,
          elapsed: elapsedStr,
        }),
        'loading',
      );
    }
    return () => {
      dismissToast(toastId);
    };
  }, [elapsed, progress, prettyName, showPersistentToast, dismissToast, t, toastId]);

  const stage = progress?.stage;
  const percent = progress?.total_percent;
  const inlineLabel =
    stage && typeof percent === 'number' && percent > 0
      ? `${stage} (${Math.min(100, Math.max(0, Math.round(percent)))}%)`
      : t('slice.analyzingPlateFilaments');
  return (
    <div className="flex flex-col gap-1 text-bambu-gray text-sm py-2">
      <div className="flex items-center gap-2">
        <Loader2 className="w-4 h-4 animate-spin" />
        {inlineLabel}
        <span className="text-xs tabular-nums">{elapsed}s</span>
      </div>
      {elapsed >= 5 && (
        <div className="text-xs text-bambu-gray/70 pl-6">
          {t(
            'slice.analyzingPlateFilamentsHint',
            'Running a preview slice to discover which AMS slots this plate uses. Cached after — re-opening is instant.',
          )}
        </div>
      )}
    </div>
  );
}
