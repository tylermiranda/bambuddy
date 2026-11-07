/**
 * Create / copy a Print Share Link (#3461).
 *
 * Ending rules are a single exclusive choice (not a checkbox + expiry combo):
 * when the print finishes, after a fixed duration, or only on revoke.
 * After create we copy via the LAN-safe clipboard helper and keep the URL
 * visible so the operator can copy again if needed.
 */
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Check, Copy, Loader2, Share2 } from 'lucide-react';
import { Button } from './Button';
import { Card, CardContent } from './Card';
import { copyTextToClipboard } from '../utils/clipboard';
import type { PrintShareResponse } from '../api/client';

export type PrintShareFormValues = {
  title: string;
  project_url: string;
  end_with_print: boolean;
  /** Hours until hard expiry; null = never. */
  expires_in_hours: number | null;
};

/** How the guest link ends — exclusive UI modes mapped onto the API flags. */
type EndMode = 'print' | 'duration' | 'revoke';

type Props = {
  printerName: string;
  defaultTitle: string;
  defaultProjectUrl?: string | null;
  isPrinting?: boolean;
  isSubmitting: boolean;
  onSubmit: (values: PrintShareFormValues) => void;
  onClose: () => void;
  createdShare?: PrintShareResponse | null;
};

const DURATION_OPTIONS: { value: number; labelKey: string; fallback: string }[] = [
  { value: 1, labelKey: 'printShare.expiry.1h', fallback: '1 hour' },
  { value: 4, labelKey: 'printShare.expiry.4h', fallback: '4 hours' },
  { value: 12, labelKey: 'printShare.expiry.12h', fallback: '12 hours' },
  { value: 24, labelKey: 'printShare.expiry.1d', fallback: '1 day' },
  { value: 72, labelKey: 'printShare.expiry.3d', fallback: '3 days' },
  { value: 168, labelKey: 'printShare.expiry.7d', fallback: '7 days' },
  { value: 720, labelKey: 'printShare.expiry.30d', fallback: '30 days' },
];

function resolveShareUrl(share: PrintShareResponse): string {
  if (share.url?.startsWith('http')) return share.url;
  return `${window.location.origin}${share.path}`;
}

export function PrintShareModal({
  printerName,
  defaultTitle,
  defaultProjectUrl,
  isPrinting = true,
  isSubmitting,
  onSubmit,
  onClose,
  createdShare,
}: Props) {
  const { t } = useTranslation();
  const [title, setTitle] = useState(defaultTitle);
  const [projectUrl, setProjectUrl] = useState(defaultProjectUrl?.trim() || '');
  const [endMode, setEndMode] = useState<EndMode>(() => (isPrinting ? 'print' : 'revoke'));
  const [durationHours, setDurationHours] = useState(24);
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);

  const shareUrl = useMemo(
    () => (createdShare ? resolveShareUrl(createdShare) : ''),
    [createdShare],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !isSubmitting) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, isSubmitting]);

  useEffect(() => {
    if (!shareUrl) return;
    let cancelled = false;
    (async () => {
      const ok = await copyTextToClipboard(shareUrl);
      if (cancelled) return;
      setCopied(ok);
      setCopyFailed(!ok);
    })();
    return () => {
      cancelled = true;
    };
  }, [shareUrl]);

  const handleCopy = async () => {
    if (!shareUrl) return;
    const ok = await copyTextToClipboard(shareUrl);
    setCopied(ok);
    setCopyFailed(!ok);
  };

  const handleCreate = () => {
    const end_with_print = isPrinting && endMode === 'print';
    const expires_in_hours = endMode === 'duration' ? durationHours : null;
    onSubmit({
      title: title.trim(),
      project_url: projectUrl.trim(),
      end_with_print,
      expires_in_hours,
    });
  };

  const radioClass = 'flex items-start gap-2 text-sm text-zinc-300 cursor-pointer';

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
      <Card className="max-w-lg w-full border border-bambu-green/30">
        <CardContent className="p-6 space-y-4">
          <div className="flex items-start gap-3">
            <Share2 className="w-6 h-6 text-bambu-green flex-shrink-0 mt-0.5" />
            <div>
              <h2 className="text-lg font-semibold text-white">
                {createdShare
                  ? t('printShare.readyTitle', 'Share link ready')
                  : isPrinting
                    ? t('printShare.confirmTitle', 'Share this print?')
                    : t('printShare.confirmPrinterTitle', 'Share printer status?')}
              </h2>
              <p className="text-sm text-zinc-400 mt-1">
                {createdShare
                  ? t(
                      'printShare.readyHint',
                      'Anyone with this link can watch {{name}} until the share ends.',
                      { name: printerName },
                    )
                  : t(
                      'printShare.confirmMessage',
                      'Guest link for live camera and progress on {{name}}. The URL is copied when you create it.',
                      { name: printerName },
                    )}
              </p>
            </div>
          </div>

          {createdShare ? (
            <>
              <div className="space-y-2">
                <label className="text-sm text-zinc-300">
                  {t('printShare.shareUrl', 'Share URL')}
                </label>
                <div className="flex gap-2">
                  <input
                    readOnly
                    value={shareUrl}
                    className="flex-1 bg-bambu-dark-tertiary border border-bambu-dark-tertiary rounded px-3 py-2 text-sm text-white font-mono"
                    onFocus={(e) => e.currentTarget.select()}
                  />
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={handleCopy}
                    title={t('common.copyToClipboard', 'Copy')}
                  >
                    {copied ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
                  </Button>
                </div>
                {copied && (
                  <p className="text-xs text-emerald-400">
                    {t('printShare.copied', 'Share link copied')}
                  </p>
                )}
                {copyFailed && (
                  <p className="text-xs text-amber-400">
                    {t(
                      'printShare.copyFailed',
                      'Automatic copy failed — select the URL and copy it manually.',
                    )}
                  </p>
                )}
              </div>
              <div className="flex justify-end gap-2 pt-2">
                <Button variant="primary" onClick={onClose}>
                  {t('common.close', 'Close')}
                </Button>
              </div>
            </>
          ) : (
            <>
              <div className="space-y-3">
                <div>
                  <label className="block text-sm text-zinc-300 mb-1">
                    {t('printShare.fieldTitle', 'Title')}
                  </label>
                  <input
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                    maxLength={200}
                    className="w-full bg-bambu-dark-tertiary border border-bambu-dark-tertiary rounded px-3 py-2 text-sm text-white"
                    placeholder={defaultTitle || t('printShare.untitled', 'Print')}
                  />
                </div>
                <div>
                  <label className="block text-sm text-zinc-300 mb-1">
                    {t('printShare.fieldProjectUrl', 'Project / model link')}
                  </label>
                  <input
                    value={projectUrl}
                    onChange={(e) => setProjectUrl(e.target.value)}
                    maxLength={500}
                    type="url"
                    inputMode="url"
                    className="w-full bg-bambu-dark-tertiary border border-bambu-dark-tertiary rounded px-3 py-2 text-sm text-white"
                    placeholder="https://makerworld.com/…"
                  />
                  <p className="text-xs text-zinc-500 mt-1">
                    {t(
                      'printShare.fieldProjectUrlHint',
                      'Optional. MakerWorld, Printables, etc. Leave blank if there is no public project.',
                    )}
                  </p>
                </div>

                <fieldset className="space-y-2">
                  <legend className="text-sm text-zinc-300 mb-1">
                    {t('printShare.fieldEndsWhen', 'Share ends')}
                  </legend>

                  <label className={`${radioClass} ${!isPrinting ? 'opacity-50 cursor-not-allowed' : ''}`}>
                    <input
                      type="radio"
                      name="print-share-end"
                      className="mt-1"
                      disabled={!isPrinting}
                      checked={endMode === 'print'}
                      onChange={() => isPrinting && setEndMode('print')}
                    />
                    <span>
                      <span className="block text-white">
                        {t('printShare.endMode.print', 'When this print finishes')}
                      </span>
                      <span className="block text-xs text-zinc-500">
                        {isPrinting
                          ? t(
                              'printShare.endMode.printHint',
                              'Recommended. Live video stops when the job completes, fails, or is cancelled.',
                            )
                          : t(
                              'printShare.endMode.noActivePrintHint',
                              'Unavailable while printer is idle — choose a duration or permanent status link below.',
                            )}
                      </span>
                    </span>
                  </label>

                  <label className={radioClass}>
                    <input
                      type="radio"
                      name="print-share-end"
                      className="mt-1"
                      checked={endMode === 'duration'}
                      onChange={() => setEndMode('duration')}
                    />
                    <span className="flex-1 min-w-0">
                      <span className="block text-white mb-1">
                        {t('printShare.endMode.duration', 'After a set time')}
                      </span>
                      <select
                        value={durationHours}
                        disabled={endMode !== 'duration'}
                        onChange={(e) => setDurationHours(Number(e.target.value))}
                        onClick={() => setEndMode('duration')}
                        className="w-full bg-bambu-dark-tertiary border border-bambu-dark-tertiary rounded px-3 py-2 text-sm text-white disabled:opacity-50"
                      >
                        {DURATION_OPTIONS.map((opt) => (
                          <option key={opt.value} value={opt.value}>
                            {t(opt.labelKey, opt.fallback)}
                          </option>
                        ))}
                      </select>
                    </span>
                  </label>

                  <label className={radioClass}>
                    <input
                      type="radio"
                      name="print-share-end"
                      className="mt-1"
                      checked={endMode === 'revoke'}
                      onChange={() => setEndMode('revoke')}
                    />
                    <span>
                      <span className="block text-white">
                        {t('printShare.endMode.revoke', 'Only when I revoke it')}
                      </span>
                      <span className="block text-xs text-zinc-500">
                        {t(
                          'printShare.endMode.revokeHint',
                          'Stays live after the print ends until you delete the share.',
                        )}
                      </span>
                    </span>
                  </label>
                </fieldset>
              </div>
              <div className="flex justify-end gap-2 pt-2">
                <Button variant="secondary" onClick={onClose} disabled={isSubmitting}>
                  {t('common.cancel', 'Cancel')}
                </Button>
                <Button variant="primary" onClick={handleCreate} disabled={isSubmitting}>
                  {isSubmitting ? (
                    <>
                      <Loader2 className="w-4 h-4 animate-spin mr-2" />
                      {t('printShare.creating', 'Creating link…')}
                    </>
                  ) : (
                    t('printShare.confirmButton', 'Create & copy link')
                  )}
                </Button>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
