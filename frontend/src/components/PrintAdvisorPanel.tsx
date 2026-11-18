import { useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Flame,
  Gauge,
  Info,
  Loader2,
  Printer,
  Sparkles,
  Zap,
} from 'lucide-react';
import type {
  AdvisorFinding,
  AdvisorPriority,
  AssessResponse,
  EnhanceResponse,
} from '../api/client';
import type { ViewMode } from './GcodeToolpathViewer';

interface PrintAdvisorPanelProps {
  assess: AssessResponse | null | undefined;
  isAssessing: boolean;
  onEnhance: (priority: AdvisorPriority) => Promise<EnhanceResponse>;
  isEnhancing: boolean;
  enhanceResult: EnhanceResponse | null;
  onSelectFinding: (range: [number, number] | null, mode: ViewMode) => void;
  onOpenEnhancedFile: (newLibraryFileId: number) => void;
  onQueueEnhancedFile?: (newLibraryFileId: number, filename: string) => void;
}

const PRIORITY_OPTIONS: Array<{ value: AdvisorPriority; label: string; hint: string }> = [
  { value: 'balanced', label: 'Balanced', hint: 'Faster cool infill + hot overhang protection' },
  { value: 'fast', label: 'Fast', hint: 'Maximize safe print speed up to 90% hotend melt cap' },
  { value: 'strong', label: 'Strong', hint: 'Target ideal interlayer bond & trim excess part fan' },
  { value: 'gloss', label: 'Uniform Gloss', hint: 'Lock outer-wall speed to eliminate matte/shiny bands' },
];

function formatDurationShort(seconds: number): string {
  const abs = Math.max(0, Math.round(Math.abs(seconds)));
  const h = Math.floor(abs / 3600);
  const m = Math.floor((abs % 3600) / 60);
  const s = abs % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function severityStyles(sev: AdvisorFinding['severity']): string {
  if (sev === 'critical') return 'border-red-500/40 bg-red-500/10 text-red-300';
  if (sev === 'warning') return 'border-amber-500/40 bg-amber-500/10 text-amber-200';
  return 'border-sky-500/30 bg-sky-500/10 text-sky-200';
}

export function PrintAdvisorPanel({
  assess,
  isAssessing,
  onEnhance,
  isEnhancing,
  enhanceResult,
  onSelectFinding,
  onOpenEnhancedFile,
  onQueueEnhancedFile,
}: PrintAdvisorPanelProps) {
  const [priority, setPriority] = useState<AdvisorPriority>('balanced');
  const [enhanceError, setEnhanceError] = useState<string | null>(null);

  const handleRunEnhance = async () => {
    setEnhanceError(null);
    try {
      await onEnhance(priority);
    } catch (err) {
      setEnhanceError(err instanceof Error ? err.message : 'Optimization failed');
    }
  };

  if (isAssessing) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-sm text-bambu-gray">
        <Loader2 className="h-5 w-5 animate-spin text-bambu-green" />
        <span>Simulating thermal TQI & G-code kinematics...</span>
      </div>
    );
  }

  if (!assess) {
    return (
      <div className="p-4 text-xs text-bambu-gray">
        Thermal & G-code diagnostics unavailable for this file.
      </div>
    );
  }

  const totalLayers = Math.max(assess.layers.length, 1);
  const coldPct = Math.round((assess.tqi_cold_layers / totalLayers) * 100);
  const hotPct = Math.round((assess.tqi_hot_layers / totalLayers) * 100);
  const idealPct = Math.max(0, 100 - coldPct - hotPct);

  const scoreColor =
    assess.health_score >= 85
      ? 'text-emerald-400 border-emerald-500/40 bg-emerald-500/10'
      : assess.health_score >= 65
        ? 'text-amber-300 border-amber-500/40 bg-amber-500/10'
        : 'text-red-400 border-red-500/40 bg-red-500/10';

  const recEntries = Object.entries(assess.slicer_recommendations ?? {});

  return (
    <div className="flex flex-col gap-4 p-4 text-gray-200" data-testid="print-advisor-panel">
      {/* Header & Health Score */}
      <div className="flex items-center justify-between gap-2">
        <div>
          <div className="flex items-center gap-1.5 text-sm font-semibold text-white">
            <Flame className="h-4 w-4 text-bambu-green" />
            <span>Print Advisor</span>
            <span className="rounded bg-bambu-dark-tertiary px-1.5 py-0.5 text-[0.65rem] font-normal text-bambu-gray">
              {assess.material}
              {assess.nozzle_diameter_mm ? ` • ${assess.nozzle_diameter_mm}mm` : ''}
            </span>
          </div>
          <div className="mt-0.5 text-[0.7rem] text-bambu-gray">
            Est. {formatDurationShort(assess.estimated_time_s)}
            {assess.estimated_fast_savings_s >= 60 && (
              <span className="ml-1.5 text-bambu-green">
                • Up to -{formatDurationShort(assess.estimated_fast_savings_s)} faster
              </span>
            )}
          </div>
        </div>
        <div className={`rounded-lg border px-2.5 py-1 text-right ${scoreColor}`}>
          <div className="text-[0.6rem] uppercase tracking-wider opacity-80">Health</div>
          <div className="text-base font-bold tabular-nums">{assess.health_score}/100</div>
        </div>
      </div>

      {/* Thermal Quality Index (TQI) Distribution */}
      <div className="rounded-lg border border-bambu-dark-tertiary bg-bambu-dark/60 p-3">
        <div className="mb-1.5 flex items-center justify-between text-xs">
          <button
            type="button"
            onClick={() => onSelectFinding(null, 'tqi')}
            className="font-medium text-white hover:text-bambu-green transition-colors"
          >
            Thermal Quality Index (TQI)
          </button>
          <span className="tabular-nums text-[0.7rem] text-bambu-gray">
            Mean {assess.tqi_mean > 0 ? `+${assess.tqi_mean}` : assess.tqi_mean}
          </span>
        </div>
        <div className="flex h-2 w-full overflow-hidden rounded-full bg-zinc-800">
          {coldPct > 0 && <div style={{ width: `${coldPct}%` }} className="bg-blue-500" title={`Cold: ${coldPct}%`} />}
          {idealPct > 0 && <div style={{ width: `${idealPct}%` }} className="bg-emerald-500" title={`Ideal: ${idealPct}%`} />}
          {hotPct > 0 && <div style={{ width: `${hotPct}%` }} className="bg-red-500" title={`Hot: ${hotPct}%`} />}
        </div>
        <div className="mt-1.5 flex justify-between text-[0.65rem] text-bambu-gray">
          <span className="text-blue-400">Cold ({assess.tqi_cold_layers})</span>
          <span className="text-emerald-400">Ideal ({idealPct}%)</span>
          <span className="text-red-400">Hot ({assess.tqi_hot_layers})</span>
        </div>

        <div className="mt-2.5 flex items-center justify-between border-t border-bambu-dark-tertiary pt-2 text-[0.7rem] text-bambu-gray">
          <span className="inline-flex items-center gap-1">
            <Gauge className="h-3.5 w-3.5" /> Peak Flow
          </span>
          <span className="tabular-nums text-white">
            {assess.max_volumetric_flow_mm3s} / {assess.flow_limit_mm3s} mm³/s
          </span>
        </div>
      </div>

      {/* Findings List */}
      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between text-xs font-medium text-bambu-gray">
          <span>Diagnostic Findings ({assess.findings.length})</span>
          {assess.findings.length > 0 && (
            <button
              type="button"
              onClick={() => onSelectFinding([0, Math.max(0, assess.layers.length - 1)], 'risk')}
              className="text-[0.7rem] text-bambu-green hover:underline"
            >
              Show all risk zones
            </button>
          )}
        </div>

        {assess.findings.length === 0 ? (
          <div className="flex items-center gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-2.5 text-xs text-emerald-300">
            <CheckCircle2 className="h-4 w-4 shrink-0" />
            <span>Thermal cooling window and volumetric flow look healthy across all layers.</span>
          </div>
        ) : (
          assess.findings.map((f, idx) => (
            <button
              key={`${f.code}-${idx}`}
              type="button"
              onClick={() => {
                if (f.layer_start != null) {
                  onSelectFinding(
                    [f.layer_start, f.layer_end ?? f.layer_start],
                    f.code === 'overheating_curl' || f.code === 'cold_delamination' ? 'tqi' : 'risk',
                  );
                }
              }}
              className={`flex flex-col gap-1 rounded-lg border p-2.5 text-left text-xs transition-opacity hover:opacity-90 ${severityStyles(f.severity)}`}
            >
              <div className="flex items-start gap-1.5 font-medium">
                {f.severity === 'info' ? (
                  <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                ) : (
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                )}
                <span>{f.title}</span>
              </div>
              <p className="text-[0.7rem] opacity-90">{f.detail}</p>
              {f.slicer_fix && (
                <div className="mt-0.5 text-[0.68rem] opacity-75">Tip: {f.slicer_fix}</div>
              )}
            </button>
          ))
        )}
      </div>

      {/* Enhance G-Code Optimizer */}
      <div className="rounded-lg border border-bambu-dark-tertiary bg-bambu-dark/60 p-3">
        <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-white">
          <Sparkles className="h-3.5 w-3.5 text-bambu-green" />
          <span>Enhance G-Code (Geometry-Safe)</span>
        </div>
        <div className="grid grid-cols-2 gap-1.5">
          {PRIORITY_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              onClick={() => setPriority(opt.value)}
              title={opt.hint}
              className={`rounded border px-2 py-1.5 text-left text-xs transition-colors ${
                priority === opt.value
                  ? 'border-bambu-green bg-bambu-green/15 text-white'
                  : 'border-bambu-dark-tertiary text-bambu-gray hover:text-white'
              }`}
            >
              <div className="font-medium">{opt.label}</div>
              <div className="line-clamp-1 text-[0.62rem] opacity-75">{opt.hint}</div>
            </button>
          ))}
        </div>

        <button
          type="button"
          disabled={isEnhancing}
          onClick={handleRunEnhance}
          className="mt-2.5 inline-flex w-full items-center justify-center gap-1.5 rounded bg-bambu-green px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-bambu-green/90 disabled:opacity-50"
        >
          {isEnhancing ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Optimizing G-code & .gcode.md5...
            </>
          ) : (
            <>
              <Zap className="h-3.5 w-3.5" />
              Create Optimized 3MF ({PRIORITY_OPTIONS.find((o) => o.value === priority)?.label})
            </>
          )}
        </button>

        {enhanceError && (
          <div className="mt-2 text-[0.7rem] text-red-400">{enhanceError}</div>
        )}

        {enhanceResult && (
          <div className="mt-3 flex flex-col gap-2 rounded border border-bambu-green/40 bg-bambu-green/10 p-2.5 text-xs">
            <div className="font-medium text-white">
              Saved: <span className="break-all text-bambu-green">{enhanceResult.filename}</span>
            </div>
            <div className="flex items-center justify-between text-[0.7rem] text-gray-200">
              <span>
                Time: {formatDurationShort(enhanceResult.before_time_s)} →{' '}
                <strong>{formatDurationShort(enhanceResult.after_time_s)}</strong>
                {enhanceResult.time_saved_s !== 0 && (
                  <span className="ml-1 text-bambu-green">
                    ({enhanceResult.time_saved_s > 0 ? '-' : '+'}
                    {formatDurationShort(Math.abs(enhanceResult.time_saved_s))})
                  </span>
                )}
              </span>
              <span>
                Health: {enhanceResult.before_health_score} →{' '}
                <strong>{enhanceResult.after_health_score}</strong>
              </span>
            </div>
            <ul className="list-inside list-disc text-[0.68rem] text-gray-300">
              {enhanceResult.changes_summary.map((line, i) => (
                <li key={i}>{line}</li>
              ))}
            </ul>
            <div className="mt-1 flex gap-1.5">
              <button
                type="button"
                onClick={() => onOpenEnhancedFile(enhanceResult.new_library_file_id)}
                className="flex-1 rounded border border-bambu-dark-tertiary bg-bambu-dark px-2 py-1 text-[0.7rem] text-white hover:border-bambu-green"
              >
                Inspect in Viewer
              </button>
              {onQueueEnhancedFile && (
                <button
                  type="button"
                  onClick={() => onQueueEnhancedFile(enhanceResult.new_library_file_id, enhanceResult.filename)}
                  className="inline-flex flex-1 items-center justify-center gap-1 rounded bg-bambu-green px-2 py-1 text-[0.7rem] font-medium text-white hover:bg-bambu-green/90"
                >
                  <Printer className="h-3 w-3" />
                  Queue Print
                </button>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Slicer Recommendations */}
      {recEntries.length > 0 && (
        <div className="rounded-lg border border-bambu-dark-tertiary bg-bambu-dark/40 p-2.5">
          <div className="mb-1.5 text-[0.7rem] font-medium text-bambu-gray">
            Suggested Slicer Preset Overrides
          </div>
          <div className="flex flex-col gap-1 text-[0.68rem]">
            {recEntries.map(([k, v]) => (
              <div key={k} className="flex items-center justify-between gap-2 font-mono">
                <span className="truncate text-bambu-gray">{k}</span>
                <span className="text-bambu-green">{String(v)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
