import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { ArrowLeft, Flame } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { api, type AdvisorPriority, type EnhanceResponse } from '../api/client';
import { GcodeToolpathViewer, type ViewMode } from '../components/GcodeToolpathViewer';
import { PrintAdvisorPanel } from '../components/PrintAdvisorPanel';
import { PrintModal } from '../components/PrintModal';

/**
 * Full-page G-code preview + Print Advisor ("Assess & Enhance").
 */
export function GCodeViewerPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const { t } = useTranslation();

  const archiveId = searchParams.get('archive');
  const libraryFileId = searchParams.get('library_file');
  const plate = searchParams.get('plate');
  const initialAdvisorOpen = searchParams.get('advisor') === '1';

  const [advisorOpen, setAdvisorOpen] = useState<boolean>(initialAdvisorOpen);
  const [externalLayerRange, setExternalLayerRange] = useState<[number, number] | null>(null);
  const [requestedViewMode, setRequestedViewMode] = useState<ViewMode | null>(
    initialAdvisorOpen ? 'tqi' : null,
  );
  const [enhanceResult, setEnhanceResult] = useState<EnhanceResponse | null>(null);
  const [queueTarget, setQueueTarget] = useState<{ id: number; name: string } | null>(null);

  const archiveColorsQuery = useQuery({
    queryKey: ['gcode-viewer-archive-colors', archiveId],
    queryFn: () => api.getArchiveCapabilities(Number(archiveId)),
    enabled: Boolean(archiveId),
    staleTime: 5 * 60_000,
    retry: false,
  });

  const libraryPlatesQuery = useQuery({
    queryKey: ['gcode-viewer-library-colors', libraryFileId],
    queryFn: () => api.getLibraryFilePlates(Number(libraryFileId)),
    enabled: Boolean(libraryFileId),
    staleTime: 5 * 60_000,
    retry: false,
  });

  const archivePlatesQuery = useQuery({
    queryKey: ['gcode-viewer-archive-plates', archiveId],
    queryFn: () => api.getArchivePlates(Number(archiveId)),
    enabled: Boolean(archiveId),
    staleTime: 5 * 60_000,
    retry: false,
  });

  const plates = useMemo(
    () => (archiveId ? archivePlatesQuery.data?.plates : libraryPlatesQuery.data?.plates) ?? [],
    [archiveId, archivePlatesQuery.data, libraryPlatesQuery.data],
  );

  const activePlate = useMemo(() => {
    if (plate) return Number(plate);
    if (plates.length === 0) return null;
    return Math.min(...plates.map((p) => p.index));
  }, [plate, plates]);

  const assessQuery = useQuery({
    queryKey: ['print-advisor-assess', archiveId ?? null, libraryFileId ?? null, activePlate],
    queryFn: async () => {
      try {
        const params = activePlate ? { plate: activePlate } : undefined;
        if (libraryFileId) return await api.assessLibraryFile(Number(libraryFileId), params);
        if (archiveId) return await api.assessArchive(Number(archiveId), params);
        return null;
      } catch {
        return null;
      }
    },
    enabled: Boolean(archiveId || libraryFileId),
    staleTime: 5 * 60_000,
    retry: false,
  });

  const enhanceMutation = useMutation({
    mutationFn: async (priority: AdvisorPriority) => {
      const body = { priority, plate: activePlate };
      if (libraryFileId) return api.enhanceLibraryFile(Number(libraryFileId), body);
      if (archiveId) return api.enhanceArchive(Number(archiveId), body);
      throw new Error('No source file selected');
    },
    onSuccess: (res) => {
      setEnhanceResult(res);
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
    },
  });

  const selectPlate = (index: number) => {
    if (index === activePlate) return;
    setEnhanceResult(null);
    setExternalLayerRange(null);
    const next = new URLSearchParams(searchParams);
    next.set('plate', String(index));
    setSearchParams(next, { replace: true });
  };

  const filamentColors = useMemo<string[] | undefined>(() => {
    if (archiveId) return archiveColorsQuery.data?.filament_colors;

    const source = plates.find((p) => p.index === activePlate) || plates[0];
    if (!source?.filaments?.length) return undefined;

    const colors: string[] = [];
    for (const filament of source.filaments) {
      const slot = Math.max(0, (filament.slot_id ?? 1) - 1);
      if (filament.color) colors[slot] = filament.color;
    }
    return colors.length > 0 ? colors : undefined;
  }, [archiveId, archiveColorsQuery.data, plates, activePlate]);

  const gcodeUrl = useMemo(() => {
    const withPlate = (base: string) => (plate ? `${base}?plate=${encodeURIComponent(plate)}` : base);
    if (archiveId) return withPlate(api.getArchiveGcode(Number(archiveId)));
    if (libraryFileId) return withPlate(api.getLibraryFileGcodeUrl(Number(libraryFileId)));
    return null;
  }, [archiveId, libraryFileId, plate]);

  const handleBack = () => {
    if (window.history.length > 1) navigate(-1);
    else navigate(archiveId ? '/archives' : '/files');
  };

  const backLabel = archiveId
    ? t('gcodeViewer.backToArchives', 'Back to Archives')
    : t('gcodeViewer.backToFiles', 'Back to File Manager');

  return (
    <div className="relative flex flex-col min-h-[calc(100vh-64px)] lg:h-screen">
      <div className="flex-shrink-0 px-4 py-2 border-b border-bambu-dark-tertiary flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <button
            type="button"
            onClick={handleBack}
            className="inline-flex items-center gap-1.5 text-sm text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white transition-colors"
          >
            <ArrowLeft className="w-4 h-4" />
            {backLabel}
          </button>

          {plates.length > 1 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs text-bambu-gray">{t('gcodeViewer.plates', 'Plates')}</span>
              {plates.map((p) => (
                <button
                  key={p.index}
                  type="button"
                  onClick={() => selectPlate(p.index)}
                  aria-pressed={p.index === activePlate}
                  title={p.name ?? undefined}
                  className={`px-2 py-0.5 rounded text-xs transition-colors ${
                    p.index === activePlate
                      ? 'bg-bambu-green text-white'
                      : 'bg-bambu-dark-tertiary text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white'
                  }`}
                >
                  {t('gcodeViewer.plateN', 'Plate {{n}}', { n: p.index })}
                </button>
              ))}
            </div>
          )}
        </div>

        {(archiveId || libraryFileId) && (
          <button
            type="button"
            onClick={() => setAdvisorOpen((v) => !v)}
            aria-pressed={advisorOpen}
            className={`inline-flex items-center gap-1.5 rounded px-2.5 py-1 text-xs font-medium transition-colors ${
              advisorOpen
                ? 'bg-bambu-green text-white'
                : 'border border-bambu-dark-tertiary bg-bambu-dark-secondary text-gray-200 hover:border-bambu-green'
            }`}
          >
            <Flame className="h-3.5 w-3.5" />
            <span>
              Print Advisor
              {assessQuery.data ? ` (${assessQuery.data.health_score}/100)` : ''}
            </span>
          </button>
        )}
      </div>

      {gcodeUrl ? (
        <>
          <GcodeToolpathViewer
            gcodeUrl={gcodeUrl}
            filamentColors={filamentColors}
            advisorLayers={assessQuery.data?.layers}
            externalLayerRange={externalLayerRange}
            requestedViewMode={requestedViewMode}
            className="flex-1 min-h-0"
          />
          {advisorOpen && (
            <aside className="absolute right-0 top-[45px] bottom-0 z-20 w-full sm:w-96 border-l border-bambu-dark-tertiary bg-bambu-dark-secondary/95 backdrop-blur overflow-y-auto shadow-xl">
              <PrintAdvisorPanel
                assess={assessQuery.data}
                isAssessing={assessQuery.isLoading}
                onEnhance={(priority) => enhanceMutation.mutateAsync(priority)}
                isEnhancing={enhanceMutation.isPending}
                enhanceResult={enhanceResult}
                onSelectFinding={(range, mode) => {
                  setRequestedViewMode(mode);
                  if (range) setExternalLayerRange(range);
                }}
                onOpenEnhancedFile={(newId) => {
                  setEnhanceResult(null);
                  navigate(`/gcode-viewer?library_file=${newId}&advisor=1`);
                }}
                onQueueEnhancedFile={(newId, fname) => setQueueTarget({ id: newId, name: fname })}
              />
            </aside>
          )}
        </>
      ) : (
        <div className="flex-1 flex items-center justify-center text-bambu-gray">
          {t('gcodeViewer.noSource', 'No G-code source specified.')}
        </div>
      )}

      {queueTarget && (
        <PrintModal
          mode="create"
          libraryFileId={queueTarget.id}
          archiveName={queueTarget.name}
          onClose={() => setQueueTarget(null)}
          onSuccess={() => setQueueTarget(null)}
        />
      )}
    </div>
  );
}
