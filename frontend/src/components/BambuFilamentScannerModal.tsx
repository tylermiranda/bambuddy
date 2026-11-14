import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQueryClient } from '@tanstack/react-query';
import {
  Barcode,
  Camera,
  Check,
  Edit3,
  Loader2,
  Package,
  Plus,
  RotateCcw,
  Search,
  Sparkles,
  X,
} from 'lucide-react';
import {
  api,
  type BambuCatalogFilament,
  type BambuScanResponse,
  type StorageLocation,
} from '../api/client';
import { QrSpoolScanner } from './QrSpoolScanner';
import { FilamentSwatch } from './FilamentSwatch';
import { useToast } from '../contexts/ToastContext';
import { invalidateSpoolAndLocationQueries } from '../utils/inventoryQueries';

export interface BambuSpoolPrefill {
  brand: string;
  material: string;
  subtype: string | null;
  color_name: string;
  rgba: string;
  extra_colors: string | null;
  effect_type: string | null;
  label_weight: number;
  core_weight: number;
  core_weight_catalog_id: number | null;
  slicer_filament: string;
  slicer_filament_name: string;
  location_id: number | null;
  storage_location: string | null;
  material_number: string;
  note: string | null;
  quantity: number;
}

interface BambuFilamentScannerModalProps {
  onClose: () => void;
  onOpenInForm: (prefill: BambuSpoolPrefill) => void;
  locations: StorageLocation[];
  spoolmanMode?: boolean;
  spoolsQueryKey?: string[];
}

export function BambuFilamentScannerModal({
  onClose,
  onOpenInForm,
  locations,
  spoolmanMode = false,
  spoolsQueryKey = ['inventory-spools'],
}: BambuFilamentScannerModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [inputText, setInputText] = useState('');
  const [scanResult, setScanResult] = useState<BambuScanResponse | null>(null);
  const [selectedFilament, setSelectedFilament] = useState<BambuCatalogFilament | null>(null);
  const [suggestions, setSuggestions] = useState<BambuCatalogFilament[]>([]);
  const [isRefill, setIsRefill] = useState(false);
  const [labelWeight, setLabelWeight] = useState(1000);
  const [plasticCoreWeight, setPlasticCoreWeight] = useState(250);
  const [plasticCatalogId, setPlasticCatalogId] = useState<number | null>(null);
  const [quantity, setQuantity] = useState(1);
  const [locationId, setLocationId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showCamera, setShowCamera] = useState(true);
  const [addedCount, setAddedCount] = useState(0);

  // Load initial catalog suggestions or filter as the user types
  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(() => {
      void api
        .searchBambuCatalog(inputText.trim(), 24)
        .then((res) => {
          if (!cancelled) setSuggestions(res.filaments);
        })
        .catch(() => {});
    }, 150);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [inputText]);

  const applyScanResponse = (res: BambuScanResponse) => {
    setScanResult(res);
    setIsRefill(res.is_refill);
    setLabelWeight(res.label_weight || 1000);
    if (!res.is_refill && res.core_weight > 0) {
      setPlasticCoreWeight(res.core_weight);
      setPlasticCatalogId(res.core_weight_catalog_id);
    }
    if (res.matched && res.filament) {
      setSelectedFilament(res.filament);
      setShowCamera(false);
      setError(null);
    } else if (res.match_type === 'unmapped_barcode') {
      setSelectedFilament(null);
      setShowCamera(false);
      setError(null);
      if (res.candidates?.length) {
        setSuggestions(res.candidates);
      }
    } else if (res.candidates?.length) {
      setSelectedFilament(null);
      setSuggestions(res.candidates);
      setError(
        t(
          'inventory.bambuScan.selectMatchBelow',
          'Multiple Bambu filaments matched — tap the exact spool below.',
        ),
      );
    } else {
      setError(
        t(
          'inventory.bambuScan.noMatch',
          `Could not match "${res.raw_input}". Try typing the 5-digit Bambu code on the box (e.g. 10100) or color name.`,
        ),
      );
    }
  };

  const handleResolveInput = async (raw: string) => {
    const trimmed = raw.trim();
    if (!trimmed) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.scanBambuFilament(trimmed);
      applyScanResponse(res);
    } catch (err) {
      setError((err as Error).message || 'Failed to resolve Bambu code');
    } finally {
      setBusy(false);
    }
  };

  const handlePhotoFallback = async (file: File): Promise<boolean> => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.scanBambuImage(file);
      if (res.matched || res.match_type === 'unmapped_barcode' || (res.decoded_barcodes?.length ?? 0) > 0) {
        applyScanResponse(res);
        return true;
      }
      return false;
    } catch {
      return false;
    } finally {
      setBusy(false);
    }
  };

  const handlePickCatalogItem = async (item: BambuCatalogFilament) => {
    // If the user just scanned an unmapped retail barcode, link it for future 1-tap scans!
    if (scanResult?.match_type === 'unmapped_barcode' && scanResult.detected_barcode) {
      setBusy(true);
      try {
        const learned = await api.learnBambuBarcode({
          barcode: scanResult.detected_barcode,
          filament_code: item.filament_code,
          is_refill: isRefill,
          label_weight: labelWeight,
        });
        showToast(
          t(
            'inventory.bambuScan.barcodeLinked',
            `Linked barcode ${scanResult.detected_barcode} to Bambu ${item.fila_type} ${item.color_name} (#${item.filament_code})`,
          ),
          'success',
        );
        applyScanResponse(learned);
        return;
      } catch {
        /* Still allow selecting the item even if saving barcode mapping failed */
      } finally {
        setBusy(false);
      }
    }

    setSelectedFilament(item);
    setShowCamera(false);
    setError(null);
  };

  const buildPrefill = (filament: BambuCatalogFilament): BambuSpoolPrefill => {
    const matchedLoc = locationId !== null ? locations.find((l) => l.id === locationId) : undefined;
    return {
      brand: 'Bambu Lab',
      material: filament.material,
      subtype: filament.subtype,
      color_name: filament.color_name,
      rgba: filament.rgba,
      extra_colors: filament.extra_colors,
      effect_type: filament.effect_type,
      label_weight: labelWeight,
      core_weight: isRefill ? 0 : plasticCoreWeight,
      core_weight_catalog_id: isRefill ? null : plasticCatalogId,
      slicer_filament: filament.filament_id,
      slicer_filament_name: filament.slicer_filament_name,
      location_id: locationId,
      storage_location: matchedLoc?.name ?? null,
      material_number: filament.filament_code,
      note: `Bambu Code: ${filament.filament_code}`,
      quantity,
    };
  };

  const handleQuickAdd = async (closeAfter: boolean) => {
    if (!selectedFilament || saving) return;
    setSaving(true);
    setError(null);
    try {
      const prefill = buildPrefill(selectedFilament);
      const payload = {
        material: prefill.material,
        subtype: prefill.subtype,
        color_name: prefill.color_name,
        rgba: prefill.rgba,
        extra_colors: prefill.extra_colors,
        effect_type: prefill.effect_type,
        brand: prefill.brand,
        label_weight: prefill.label_weight,
        core_weight: prefill.core_weight,
        core_weight_catalog_id: prefill.core_weight_catalog_id,
        weight_used: 0,
        slicer_filament: prefill.slicer_filament,
        slicer_filament_name: prefill.slicer_filament_name,
        nozzle_temp_min: null,
        nozzle_temp_max: null,
        note: prefill.note,
        added_full: true,
        last_used: null,
        encode_time: null,
        tag_uid: null,
        tray_uuid: null,
        data_origin: 'bambu_scan',
        tag_type: null,
        cost_per_kg: null,
        last_scale_weight: null,
        last_weighed_at: null,
        category: null,
        low_stock_threshold_pct: null,
        storage_location: prefill.storage_location,
        location_id: prefill.location_id,
        material_number: prefill.material_number,
      };

      if (spoolmanMode) {
        if (quantity > 1) {
          await api.bulkCreateSpoolmanInventorySpools(payload, quantity);
        } else {
          await api.createSpoolmanInventorySpool(payload);
        }
      } else {
        if (quantity > 1) {
          await api.bulkCreateSpools(payload, quantity);
        } else {
          await api.createSpool(payload);
        }
      }

      await invalidateSpoolAndLocationQueries(queryClient, spoolsQueryKey);
      setAddedCount((c) => c + quantity);
      showToast(
        t(
          'inventory.bambuScan.addedToast',
          `Added ${quantity > 1 ? `${quantity}× ` : ''}Bambu Lab ${selectedFilament.fila_type} ${selectedFilament.color_name} (#${selectedFilament.filament_code})`,
        ),
        'success',
      );

      if (closeAfter) {
        onClose();
      } else {
        // Reset for scanning the next box right away
        setSelectedFilament(null);
        setScanResult(null);
        setInputText('');
        setQuantity(1);
        setShowCamera(true);
      }
    } catch (err) {
      setError((err as Error).message || 'Failed to add spool to inventory');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4"
      onClick={onClose}
    >
      <div
        className="bg-bambu-dark-secondary rounded-xl border border-bambu-dark-tertiary w-full max-w-lg max-h-[92vh] overflow-y-auto p-5 space-y-4 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <div className="w-9 h-9 rounded-lg bg-bambu-green/15 border border-bambu-green/30 flex items-center justify-center">
              <Barcode className="w-5 h-5 text-bambu-green" />
            </div>
            <div>
              <h2 className="text-base font-semibold text-white">
                {t('inventory.bambuScan.title', 'Scan Official Bambu Filament')}
              </h2>
              <p className="text-xs text-bambu-gray">
                {t(
                  'inventory.bambuScan.subtitle',
                  'Scan box barcode, SKU, or enter the 5-digit Bambu code (e.g. 10100)',
                )}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t('common.cancel')}
            className="text-bambu-gray hover:text-white transition-colors p-1"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {addedCount > 0 && (
          <div className="flex items-center justify-between px-3 py-2 rounded-lg bg-bambu-green/15 border border-bambu-green/30 text-xs text-bambu-green">
            <span className="flex items-center gap-1.5 font-medium">
              <Check className="w-4 h-4" />
              {t('inventory.bambuScan.sessionAdded', `Added ${addedCount} spool(s) in this scan session`)}
            </span>
            <button
              type="button"
              onClick={onClose}
              className="underline hover:text-white transition-colors"
            >
              {t('common.done', 'Done')}
            </button>
          </div>
        )}

        {/* Search / Barcode / 5-Digit Code Input */}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void handleResolveInput(inputText);
          }}
          className="flex gap-2"
        >
          <div className="relative flex-1">
            <Search className="w-4 h-4 text-bambu-gray absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
            <input
              type="text"
              value={inputText}
              onChange={(e) => setInputText(e.target.value)}
              placeholder={t(
                'inventory.bambuScan.inputPlaceholder',
                '5-digit code (10100), SKU (A00-W01), barcode, or color…',
              )}
              className="w-full pl-9 pr-8 py-2 rounded-lg bg-bambu-dark border border-bambu-dark-tertiary text-sm text-white placeholder-bambu-gray focus:outline-none focus:border-bambu-green"
            />
            {inputText && (
              <button
                type="button"
                onClick={() => setInputText('')}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 text-bambu-gray hover:text-white"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
          <button
            type="submit"
            disabled={!inputText.trim() || busy}
            className="px-3.5 py-2 rounded-lg bg-bambu-green text-white text-sm font-medium hover:bg-bambu-green-light transition-colors disabled:opacity-50 flex items-center gap-1.5"
          >
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
            {t('inventory.bambuScan.lookup', 'Lookup')}
          </button>
        </form>

        {/* Unmapped Retail Barcode Prompt */}
        {scanResult?.match_type === 'unmapped_barcode' && !selectedFilament && (
          <div className="p-3 rounded-lg bg-amber-500/15 border border-amber-500/40 space-y-1.5">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-amber-300">
                {t(
                  'inventory.bambuScan.newBarcodeTitle',
                  `Retail Barcode Scanned: ${scanResult.detected_barcode}`,
                )}
              </span>
              <button
                type="button"
                onClick={() => {
                  setScanResult(null);
                  setShowCamera(true);
                }}
                className="text-xs text-amber-200 hover:text-white underline"
              >
                {t('inventory.bambuScan.rescan', 'Rescan')}
              </button>
            </div>
            <p className="text-xs text-amber-100/90">
              {t(
                'inventory.bambuScan.newBarcodeHint',
                'Type the 5-digit Bambu code printed on the box label (e.g. 10100) or tap the filament below once — MirandaPrint will remember this barcode automatically for future 1-tap scans.',
              )}
            </p>
          </div>
        )}

        {/* Matched Filament Preview & Quick-Add Card */}
        {selectedFilament ? (
          <div className="rounded-xl bg-bambu-dark border border-bambu-green/40 p-4 space-y-4">
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-center gap-3.5">
                <FilamentSwatch
                  rgba={selectedFilament.rgba}
                  extraColors={selectedFilament.extra_colors}
                  effectType={selectedFilament.effect_type}
                  subtype={selectedFilament.subtype}
                  effectSize="card"
                  className="w-12 h-12"
                />
                <div>
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-xs font-semibold uppercase tracking-wider text-bambu-green">
                      Bambu Lab · {selectedFilament.fila_type}
                    </span>
                    <span className="px-2 py-0.5 rounded bg-bambu-dark-secondary border border-bambu-dark-tertiary text-[11px] font-mono text-white">
                      #{selectedFilament.filament_code}
                    </span>
                    <span className="px-1.5 py-0.5 rounded bg-bambu-dark-secondary text-[11px] font-mono text-bambu-gray">
                      {selectedFilament.filament_id}
                    </span>
                  </div>
                  <h3 className="text-lg font-bold text-white mt-0.5">
                    {selectedFilament.color_name}
                  </h3>
                  <p className="text-xs text-bambu-gray">
                    {selectedFilament.slicer_filament_name} · #{selectedFilament.rgba.slice(0, 6)}
                  </p>
                </div>
              </div>
              <button
                type="button"
                onClick={() => {
                  setSelectedFilament(null);
                  setScanResult(null);
                  setShowCamera(true);
                }}
                className="flex items-center gap-1 text-xs text-bambu-gray hover:text-white px-2 py-1 rounded border border-bambu-dark-tertiary"
              >
                <RotateCcw className="w-3.5 h-3.5" />
                {t('inventory.bambuScan.scanAnother', 'Scan Another')}
              </button>
            </div>

            {/* Spool Type (With Spool vs Refill) & Weight */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-1">
              <div>
                <label className="block text-xs text-bambu-gray mb-1">
                  {t('inventory.bambuScan.spoolType', 'Spool Packaging')}
                </label>
                <div className="grid grid-cols-2 gap-1.5 bg-bambu-dark-secondary p-1 rounded-lg border border-bambu-dark-tertiary">
                  <button
                    type="button"
                    onClick={() => setIsRefill(false)}
                    className={`py-1.5 px-2 rounded-md text-xs font-medium transition-colors ${
                      !isRefill
                        ? 'bg-bambu-green text-white'
                        : 'text-bambu-gray hover:text-white'
                    }`}
                  >
                    {t('inventory.bambuScan.withSpool', `With Spool (${plasticCoreWeight}g)`)}
                  </button>
                  <button
                    type="button"
                    onClick={() => setIsRefill(true)}
                    className={`py-1.5 px-2 rounded-md text-xs font-medium transition-colors ${
                      isRefill
                        ? 'bg-bambu-green text-white'
                        : 'text-bambu-gray hover:text-white'
                    }`}
                  >
                    {t('inventory.bambuScan.refill', 'Refill (0g core)')}
                  </button>
                </div>
              </div>

              <div>
                <label className="block text-xs text-bambu-gray mb-1">
                  {t('inventory.bambuScan.netWeight', 'Filament Weight')}
                </label>
                <div className="grid grid-cols-3 gap-1.5 bg-bambu-dark-secondary p-1 rounded-lg border border-bambu-dark-tertiary">
                  {[1000, 500, 250].map((w) => (
                    <button
                      key={w}
                      type="button"
                      onClick={() => setLabelWeight(w)}
                      className={`py-1.5 px-2 rounded-md text-xs font-medium transition-colors ${
                        labelWeight === w
                          ? 'bg-bambu-green text-white'
                          : 'text-bambu-gray hover:text-white'
                      }`}
                    >
                      {w}g
                    </button>
                  ))}
                </div>
              </div>
            </div>

            {/* Quantity & Location */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-bambu-gray mb-1">
                  {t('inventory.bambuScan.quantity', 'Quantity (Spools)')}
                </label>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setQuantity((q) => Math.max(1, q - 1))}
                    className="w-9 h-9 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary text-white hover:border-bambu-gray"
                  >
                    -
                  </button>
                  <input
                    type="number"
                    min={1}
                    max={50}
                    value={quantity}
                    onChange={(e) =>
                      setQuantity(Math.max(1, Math.min(50, parseInt(e.target.value, 10) || 1)))
                    }
                    className="w-16 text-center py-1.5 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary text-sm text-white"
                  />
                  <button
                    type="button"
                    onClick={() => setQuantity((q) => Math.min(50, q + 1))}
                    className="w-9 h-9 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary text-white hover:border-bambu-gray"
                  >
                    +
                  </button>
                </div>
              </div>

              <div>
                <label className="block text-xs text-bambu-gray mb-1">
                  {t('inventory.bambuScan.location', 'Storage Location (optional)')}
                </label>
                <select
                  value={locationId ?? ''}
                  onChange={(e) => setLocationId(e.target.value ? Number(e.target.value) : null)}
                  className="w-full py-2 px-3 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary text-sm text-white focus:outline-none focus:border-bambu-green"
                >
                  <option value="">{t('inventory.bambuScan.noLocation', '— Unassigned —')}</option>
                  {locations.map((loc) => (
                    <option key={loc.id} value={loc.id}>
                      {loc.name}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {error && <p className="text-xs text-red-400">{error}</p>}

            {/* Primary Actions */}
            <div className="flex flex-col sm:flex-row gap-2 pt-1">
              <button
                type="button"
                disabled={saving}
                onClick={() => void handleQuickAdd(false)}
                className="flex-1 flex items-center justify-center gap-2 py-2.5 px-4 rounded-lg bg-bambu-green text-white text-sm font-semibold hover:bg-bambu-green-light transition-colors disabled:opacity-50"
              >
                {saving ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Plus className="w-4 h-4" />
                )}
                {t(
                  'inventory.bambuScan.addAndScanNext',
                  quantity > 1 ? `Add ${quantity} Spools & Scan Next` : 'Add to Inventory & Scan Next',
                )}
              </button>
              <button
                type="button"
                disabled={saving}
                onClick={() => void handleQuickAdd(true)}
                className="flex items-center justify-center gap-1.5 py-2.5 px-3.5 rounded-lg bg-bambu-dark-secondary border border-bambu-green/50 text-bambu-green hover:bg-bambu-green/10 text-sm font-medium transition-colors disabled:opacity-50"
              >
                <Check className="w-4 h-4" />
                {t('inventory.bambuScan.addAndDone', 'Add & Done')}
              </button>
              <button
                type="button"
                disabled={saving}
                onClick={() => onOpenInForm(buildPrefill(selectedFilament))}
                title={t('inventory.bambuScan.openInFormTooltip', 'Open in full Add Spool form')}
                className="flex items-center justify-center gap-1.5 py-2.5 px-3 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary text-bambu-gray hover:text-white text-sm transition-colors"
              >
                <Edit3 className="w-4 h-4" />
                {t('inventory.bambuScan.customize', 'Form…')}
              </button>
            </div>
          </div>
        ) : (
          <>
            {/* Camera Scanner Toggle + Live View */}
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-bambu-gray flex items-center gap-1.5">
                  <Camera className="w-3.5 h-3.5 text-bambu-green" />
                  {t('inventory.bambuScan.cameraHeading', 'Camera Barcode / Box Label Scanner')}
                </span>
                <button
                  type="button"
                  onClick={() => setShowCamera((s) => !s)}
                  className="text-xs text-bambu-gray hover:text-white underline"
                >
                  {showCamera
                    ? t('inventory.bambuScan.hideCamera', 'Hide camera')
                    : t('inventory.bambuScan.showCamera', 'Show camera')}
                </button>
              </div>

              {showCamera && (
                <QrSpoolScanner
                  onDecode={(decoded) => {
                    setInputText(decoded);
                    void handleResolveInput(decoded);
                  }}
                  busy={busy}
                  error={error}
                  includeBarcodes
                  onPhotoFile={handlePhotoFallback}
                  instructionText={t(
                    'inventory.bambuScan.scanInstruction',
                    'Point camera at the Bambu box barcode/QR, or tap Take Photo for small box labels',
                  )}
                />
              )}
            </div>

            {/* Instant Official Bambu Catalog Picker */}
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-bambu-gray flex items-center gap-1.5">
                  <Package className="w-3.5 h-3.5 text-bambu-green" />
                  {inputText.trim()
                    ? t('inventory.bambuScan.matchingFilaments', 'Matching Official Bambu Filaments')
                    : t('inventory.bambuScan.popularFilaments', 'Official Bambu Lab Catalog (tap to select)')}
                </span>
                <span className="text-[11px] text-bambu-gray">
                  {t('inventory.bambuScan.catalogCount', '344 filaments')}
                </span>
              </div>

              <div className="max-h-52 overflow-y-auto divide-y divide-bambu-dark-tertiary rounded-lg border border-bambu-dark-tertiary bg-bambu-dark">
                {suggestions.length === 0 ? (
                  <p className="p-3 text-xs text-bambu-gray text-center">
                    {t('inventory.bambuScan.noSuggestions', 'No matching Bambu filaments found.')}
                  </p>
                ) : (
                  suggestions.map((item) => (
                    <button
                      key={`${item.filament_code}-${item.fila_type}-${item.color_name}`}
                      type="button"
                      onClick={() => void handlePickCatalogItem(item)}
                      className="w-full flex items-center justify-between gap-3 px-3 py-2 text-left hover:bg-bambu-dark-secondary transition-colors"
                    >
                      <div className="flex items-center gap-2.5 min-w-0">
                        <FilamentSwatch
                          rgba={item.rgba}
                          extraColors={item.extra_colors}
                          effectType={item.effect_type}
                          subtype={item.subtype}
                          effectSize="table"
                          className="w-5 h-5"
                        />
                        <div className="min-w-0">
                          <div className="text-xs font-medium text-white truncate">
                            {item.fila_type} — <span className="font-semibold">{item.color_name}</span>
                          </div>
                          <div className="text-[11px] text-bambu-gray truncate">
                            {item.slicer_filament_name} · {item.variant_ids?.[0] ?? item.filament_id}
                          </div>
                        </div>
                      </div>
                      <span className="px-2 py-0.5 rounded bg-bambu-dark-secondary border border-bambu-dark-tertiary text-xs font-mono text-bambu-green shrink-0">
                        #{item.filament_code}
                      </span>
                    </button>
                  ))
                )}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
