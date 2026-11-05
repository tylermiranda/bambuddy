/**
 * Create / copy a Print Share Link (#3461).
 *
 * Mirrors the useful OctoEverywhere Live Link knobs: title, project/model URL,
 * end-when-print-completes, and an optional hard expiry. After create we always
 * attempt a clipboard write (LAN HTTP needs the execCommand fallback) and keep
 * the URL on screen so the operator can copy again if the browser blocks it.
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
  /** Hours until hard expiry; null = never (subject to end_with_print). */
  expires_in_hours: number | null;
};

type Props = {
  printerName: string;
  defaultTitle: string;
  defaultProjectUrl?: string | null;
  isSubmitting: boolean;
  onSubmit: (values: PrintShareFormValues) => void;
  onClose: () => void;
  /** After a successful create, parent passes the share so we show copy UI. */
  createdShare?: PrintShareResponse | null;
};

const EXPIRY_OPTIONS: { value: number | null; labelKey: string; fallback: string }[] = [
  { value: 1, labelKey: 'printShare.expiry.1h', fallback: '1 hour' },
  { value: 4, labelKey: 'printShare.expiry.4h', fallback: '4 hours' },
  { value: 12, labelKey: 'printShare.expiry.12h', fallback: '12 hours' },
  { value: 24, labelKey: 'printShare.expiry.1d', fallback: '1 day' },
  { value: 72, labelKey: 'printShare.expiry.3d', fallback: '3 days' },
  { value: 168, labelKey: 'printShare.expiry.7d', fallback: '7 days' },
  { value: 720, labelKey: 'printShare.expiry.30d', fallback: '30 days' },
  { value: null, labelKey: 'printShare.expiry.never', fallback: 'Never' },
];

function resolveShareUrl(share: PrintShareResponse): string {
  if (share.url?.startsWith('http')) return share.url;
  return `${window.location.origin}${share.path}`;
}

export function PrintShareModal({
  printerName,
  defaultTitle,
  defaultProjectUrl,
  isSubmitting,
  onSubmit,
  onClose,
  createdShare,
}: Props) {
  const { t } = useTranslation();
  const [title, setTitle] = useState(defaultTitle);
  const [projectUrl, setProjectUrl] = useState(defaultProjectUrl?.trim() || '');
  const [endWithPrint, setEndWithPrint] = useState(true);
  const [expiresInHours, setExpiresInHours] = useState<number | null>(null);
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

  // Auto-copy once when the share appears.
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
    onSubmit({
      title: title.trim(),
      project_url: projectUrl.trim(),
      end_with_print: endWithPrint,
      expires_in_hours: expiresInHours,
    });
  };

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
                  : t('printShare.confirmTitle', 'Share this print?')}
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
                      'Create a guest link for the live camera and progress on {{name}}. Configure the options below — the URL is copied when the link is created.',
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
                <label className="flex items-start gap-2 text-sm text-zinc-300 cursor-pointer">
                  <input
                    type="checkbox"
                    className="mt-1"
                    checked={endWithPrint}
                    onChange={(e) => setEndWithPrint(e.target.checked)}
                  />
                  <span>
                    {t(
                      'printShare.fieldEndWithPrint',
                      'End the live stream when this print completes',
                    )}
                  </span>
                </label>
                <div>
                  <label className="block text-sm text-zinc-300 mb-1">
                    {t('printShare.fieldExpiry', 'Link expiration')}
                  </label>
                  <select
                    value={expiresInHours === null ? 'never' : String(expiresInHours)}
                    onChange={(e) => {
                      const v = e.target.value;
                      setExpiresInHours(v === 'never' ? null : Number(v));
                    }}
                    className="w-full bg-bambu-dark-tertiary border border-bambu-dark-tertiary rounded px-3 py-2 text-sm text-white"
                  >
                    {EXPIRY_OPTIONS.map((opt) => (
                      <option
                        key={String(opt.value)}
                        value={opt.value === null ? 'never' : String(opt.value)}
                      >
                        {t(opt.labelKey, opt.fallback)}
                      </option>
                    ))}
                  </select>
                  <p className="text-xs text-zinc-500 mt-1">
                    {endWithPrint
                      ? t(
                          'printShare.fieldExpiryHintEndPrint',
                          'Hard stop even if the print is still running. “Never” relies on print end / revoke.',
                        )
                      : t(
                          'printShare.fieldExpiryHint',
                          'Guest access ends at this time (or when you revoke the link).',
                        )}
                  </p>
                </div>
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
