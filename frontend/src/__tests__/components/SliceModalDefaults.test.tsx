import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { SliceModal } from '../../components/SliceModal';
import { SliceJobTrackerProvider } from '../../contexts/SliceJobTrackerContext';
import { api, type UnifiedPresetsResponse } from '../../api/client';

vi.mock('../../api/client', () => ({
  api: {
    getSlicerPresets: vi.fn(),
    sliceLibraryFile: vi.fn(),
    sliceArchive: vi.fn(),
    getSliceJob: vi.fn(),
    getLibraryFilePlates: vi.fn(),
    getArchivePlates: vi.fn(),
    getLibraryFileFilamentRequirements: vi.fn(),
    getArchiveFilamentRequirements: vi.fn(),
    getUiFlags: vi.fn(),
    getSettings: vi.fn().mockResolvedValue({}),
    updateSettings: vi.fn().mockResolvedValue({}),
    listSlicerPipelines: vi.fn(),
    createSlicerPipeline: vi.fn(),
    getSlicerPrinterModels: vi.fn(),
    getSlicerPresetValues: vi.fn(),
    getSlicerLoadedSpools: vi.fn(),
  },
}));

const mockApi = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

describe('SliceModal server-configured slicer defaults', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.listSlicerPipelines.mockResolvedValue({ pipelines: [] });
    mockApi.getSlicerLoadedSpools.mockResolvedValue({ printers: [] });
    mockApi.getSlicerPresetValues.mockResolvedValue({ resolved: true, values: {}, reason: 'ok' });
    mockApi.getSliceJob.mockResolvedValue({
      job_id: 99,
      status: 'running',
      kind: 'library_file',
      source_id: 160,
      source_name: 'Scraper.3mf',
      created_at: new Date().toISOString(),
      started_at: null,
      completed_at: null,
    });
    mockApi.getLibraryFileFilamentRequirements.mockResolvedValue({
      file_id: 160,
      filename: 'Scraper.3mf',
      plate_id: 1,
      filaments: [],
    });
  });

  it('pre-selects configured default printer + fallback process and ticks carryable designer settings', async () => {
    mockApi.getUiFlags.mockResolvedValue({
      slicer_default_printer_preset: 'Bambu Lab P2S 0.4 nozzle',
      slicer_default_process_preset: '0.20mm Standard @BBL P2S',
      slicer_use_designer_settings: true,
    });
    mockApi.getSlicerPrinterModels.mockResolvedValue({
      'Bambu Lab P2S': 'P2S',
      'Bambu Lab X1 Carbon': 'X1C',
    });
    const presets: UnifiedPresetsResponse = {
      orca_cloud: { printer: [], process: [], filament: [] },
      cloud: { printer: [], process: [], filament: [] },
      local: { printer: [], process: [], filament: [] },
      standard: {
        printer: [
          { id: 'Bambu Lab X1 Carbon 0.4 nozzle', name: 'Bambu Lab X1 Carbon 0.4 nozzle', source: 'standard' },
          { id: 'Bambu Lab P2S 0.4 nozzle', name: 'Bambu Lab P2S 0.4 nozzle', source: 'standard' },
        ],
        process: [
          { id: '0.16mm Optimal @BBL X1C', name: '0.16mm Optimal @BBL X1C', source: 'standard' },
          { id: '0.16mm Standard @BBL P2S', name: '0.16mm Standard @BBL P2S', source: 'standard' },
          { id: '0.20mm Standard @BBL P2S', name: '0.20mm Standard @BBL P2S', source: 'standard' },
        ],
        filament: [
          { id: 'Bambu PLA Basic @BBL P2S', name: 'Bambu PLA Basic @BBL P2S', source: 'standard', filament_type: 'PLA' },
        ],
      },
      cloud_status: 'ok',
      orca_cloud_status: 'ok',
    };
    mockApi.getSlicerPresets.mockResolvedValue(presets);
    mockApi.getLibraryFilePlates.mockResolvedValue({
      file_id: 160,
      filename: 'Scraper.3mf',
      plates: [{ index: 1, name: 'Plate 1', objects: ['Scraper'], object_count: 1, has_thumbnail: false, thumbnail_url: null, print_time_seconds: null, filament_used_grams: null, filaments: [] }],
      is_multi_plate: false,
      embedded_printer: 'Bambu Lab X1 Carbon 0.4 nozzle',
      embedded_process: '0.16mm Optimal @BBL X1C',
      design_overrides: [
        { key: 'sparse_infill_density', value: '50%', printer_coupled: false, preset_defining: false },
        { key: 'outer_wall_speed', value: '300', printer_coupled: true, preset_defining: false },
      ],
    });
    mockApi.sliceLibraryFile.mockResolvedValue({ job_id: 99, status: 'pending' });

    render(
      <SliceJobTrackerProvider>
        <SliceModal source={{ kind: 'libraryFile', id: 160, filename: 'Scraper.3mf' }} onClose={vi.fn()} />
      </SliceJobTrackerProvider>,
    );

    await waitFor(() => {
      const selects = (screen.getAllByRole('combobox') as HTMLSelectElement[]).filter(
        (el) => el.getAttribute('aria-label') !== 'Apply pipeline',
      );
      expect(selects[0].value).toBe('standard:Bambu Lab P2S 0.4 nozzle');
      expect(selects[1].value).toBe('standard:0.20mm Standard @BBL P2S');
    });

    const sliceBtn = screen.getByRole('button', { name: /^Slice$/i });
    await waitFor(() => expect(sliceBtn).not.toBeDisabled());
    fireEvent.click(sliceBtn);

    await waitFor(() => expect(mockApi.sliceLibraryFile).toHaveBeenCalledTimes(1));
    const payload = mockApi.sliceLibraryFile.mock.calls[0][1] as { design_overrides?: string[] };
    expect(payload.design_overrides).toEqual(['sparse_infill_density']);
  });
});
