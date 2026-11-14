import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '../utils';
import { api } from '../../api/client';
import { BambuFilamentScannerModal } from '../../components/BambuFilamentScannerModal';

const JADE_WHITE = {
  filament_code: '10100',
  filament_id: 'GFA00',
  slicer_filament_name: 'Bambu PLA Basic',
  fila_type: 'PLA Basic',
  material: 'PLA',
  subtype: 'Basic',
  color_name: 'Jade White',
  rgba: 'FFFFFFFF',
  extra_colors: null,
  effect_type: null,
  variant_ids: ['A00-W1', 'A00-W01'],
};

describe('BambuFilamentScannerModal', () => {
  beforeEach(() => {
    vi.stubGlobal('BarcodeDetector', undefined);
    vi.spyOn(api, 'searchBambuCatalog').mockResolvedValue({
      filaments: [JADE_WHITE],
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('resolves a 5-digit Bambu filament code and quick-adds a spool to inventory', async () => {
    const scanSpy = vi.spyOn(api, 'scanBambuFilament').mockResolvedValue({
      matched: true,
      match_type: 'filament_code',
      raw_input: '10100',
      detected_barcode: null,
      is_refill: false,
      label_weight: 1000,
      core_weight: 250,
      core_weight_catalog_id: 1,
      filament: JADE_WHITE,
      candidates: [],
    });
    const createSpy = vi.spyOn(api, 'createSpool').mockResolvedValue({
      id: 42,
      material: 'PLA',
      subtype: 'Basic',
      color_name: 'Jade White',
      rgba: 'FFFFFFFF',
      extra_colors: null,
      effect_type: null,
      brand: 'Bambu Lab',
      label_weight: 1000,
      core_weight: 250,
      core_weight_catalog_id: 1,
      weight_used: 0,
      slicer_filament: 'GFA00',
      slicer_filament_name: 'Bambu PLA Basic',
      nozzle_temp_min: null,
      nozzle_temp_max: null,
      note: 'Bambu Code: 10100',
      added_full: true,
      last_used: null,
      encode_time: null,
      tag_uid: null,
      tray_uuid: null,
      data_origin: 'bambu_scan',
      tag_type: null,
      archived_at: null,
      created_at: '2026-11-14T00:00:00Z',
      updated_at: '2026-11-14T00:00:00Z',
      cost_per_kg: null,
      last_scale_weight: null,
      last_weighed_at: null,
      category: null,
      low_stock_threshold_pct: null,
      material_number: '10100',
    });

    const onClose = vi.fn();
    const onOpenInForm = vi.fn();

    render(
      <BambuFilamentScannerModal
        onClose={onClose}
        onOpenInForm={onOpenInForm}
        locations={[{ id: 1, name: 'Drybox A', identifier: null, spool_count: 0, created_at: '', updated_at: '' }]}
      />,
    );

    const input = screen.getByPlaceholderText(/5-digit code/i);
    fireEvent.change(input, { target: { value: '10100' } });
    fireEvent.click(screen.getByRole('button', { name: /lookup/i }));

    await waitFor(() => expect(scanSpy).toHaveBeenCalledWith('10100'));
    expect(await screen.findByText('Jade White')).toBeInTheDocument();
    expect(screen.getByText('#10100')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /add & done/i }));

    await waitFor(() => expect(createSpy).toHaveBeenCalledTimes(1));
    expect(createSpy.mock.calls[0][0]).toMatchObject({
      brand: 'Bambu Lab',
      material: 'PLA',
      subtype: 'Basic',
      color_name: 'Jade White',
      rgba: 'FFFFFFFF',
      slicer_filament: 'GFA00',
      material_number: '10100',
      core_weight: 250,
    });
    expect(onClose).toHaveBeenCalled();
  });

  it('passes prefill to SpoolFormModal when Form button is clicked', async () => {
    vi.spyOn(api, 'scanBambuFilament').mockResolvedValue({
      matched: true,
      match_type: 'sku',
      raw_input: 'A00-W01-1.75-1000-SPLFREE',
      detected_barcode: null,
      is_refill: true,
      label_weight: 1000,
      core_weight: 0,
      core_weight_catalog_id: null,
      filament: JADE_WHITE,
      candidates: [],
    });

    const onClose = vi.fn();
    const onOpenInForm = vi.fn();

    render(
      <BambuFilamentScannerModal
        onClose={onClose}
        onOpenInForm={onOpenInForm}
        locations={[]}
      />,
    );

    const input = screen.getByPlaceholderText(/5-digit code/i);
    fireEvent.change(input, { target: { value: 'A00-W01-1.75-1000-SPLFREE' } });
    fireEvent.click(screen.getByRole('button', { name: /lookup/i }));

    expect(await screen.findByText('Jade White')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /form…/i }));

    expect(onOpenInForm).toHaveBeenCalledWith(
      expect.objectContaining({
        brand: 'Bambu Lab',
        material: 'PLA',
        subtype: 'Basic',
        color_name: 'Jade White',
        core_weight: 0,
        slicer_filament: 'GFA00',
        material_number: '10100',
      }),
    );
  });
});
