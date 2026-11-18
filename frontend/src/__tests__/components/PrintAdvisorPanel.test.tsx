import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { PrintAdvisorPanel } from '../../components/PrintAdvisorPanel';
import type { AssessResponse, EnhanceResponse } from '../../api/client';

const SAMPLE_ASSESS: AssessResponse = {
  plate_id: 1,
  material: 'PETG',
  health_score: 73,
  tqi_mean: 24.5,
  tqi_hot_layers: 6,
  tqi_cold_layers: 1,
  estimated_time_s: 3600,
  estimated_fast_savings_s: 540,
  max_volumetric_flow_mm3s: 12.6,
  flow_limit_mm3s: 13.0,
  findings: [
    {
      code: 'overheating_curl',
      severity: 'warning',
      title: 'Overheating / curling risk on 6 layer(s)',
      detail: 'Local plastic stays above softening threshold between layers 12–17.',
      layer_start: 11,
      layer_end: 16,
      slicer_fix: 'Increase minimum layer time.',
    },
  ],
  layers: Array.from({ length: 20 }, (_, i) => ({
    layer: i,
    z: (i + 1) * 0.2,
    duration_s: 180,
    tqi: i >= 11 && i <= 16 ? 58 : 4,
    max_flow_mm3s: 9.5,
    outer_wall_speed_mms: 60,
    fan_pct: 80,
    risk_flags: i >= 11 && i <= 16 ? ['overheating_curl'] : [],
  })),
  slicer_recommendations: {
    slow_down_layer_time: 8,
  },
};

describe('PrintAdvisorPanel', () => {
  it('renders health score, findings, and jumps viewer to flagged layers on click', () => {
    const onSelectFinding = vi.fn();
    render(
      <PrintAdvisorPanel
        assess={SAMPLE_ASSESS}
        isAssessing={false}
        onEnhance={vi.fn()}
        isEnhancing={false}
        enhanceResult={null}
        onSelectFinding={onSelectFinding}
        onOpenEnhancedFile={vi.fn()}
      />,
    );

    expect(screen.getByText('73/100')).toBeInTheDocument();
    expect(screen.getByText('PETG')).toBeInTheDocument();
    const findingBtn = screen.getByText(/Overheating \/ curling risk/i);
    fireEvent.click(findingBtn);
    expect(onSelectFinding).toHaveBeenCalledWith([11, 16], 'tqi');
  });

  it('runs Enhance with selected priority and renders before/after summary', async () => {
    const enhanced: EnhanceResponse = {
      new_library_file_id: 99,
      filename: 'bracket-enhanced-fast.gcode.3mf',
      priority: 'fast',
      plate_id: 1,
      before_time_s: 3600,
      after_time_s: 2940,
      time_saved_s: 660,
      before_health_score: 73,
      after_health_score: 91,
      changes_summary: ['Accelerated infill across 12 cool layer(s)'],
      assess: { ...SAMPLE_ASSESS, health_score: 91 },
    };
    const onEnhance = vi.fn().mockResolvedValue(enhanced);
    const onOpenEnhanced = vi.fn();

    const { rerender } = render(
      <PrintAdvisorPanel
        assess={SAMPLE_ASSESS}
        isAssessing={false}
        onEnhance={onEnhance}
        isEnhancing={false}
        enhanceResult={null}
        onSelectFinding={vi.fn()}
        onOpenEnhancedFile={onOpenEnhanced}
      />,
    );

    fireEvent.click(screen.getByText('Fast'));
    fireEvent.click(screen.getByRole('button', { name: /Create Optimized 3MF/i }));
    await waitFor(() => expect(onEnhance).toHaveBeenCalledWith('fast'));

    rerender(
      <PrintAdvisorPanel
        assess={SAMPLE_ASSESS}
        isAssessing={false}
        onEnhance={onEnhance}
        isEnhancing={false}
        enhanceResult={enhanced}
        onSelectFinding={vi.fn()}
        onOpenEnhancedFile={onOpenEnhanced}
      />,
    );

    expect(screen.getByText('bracket-enhanced-fast.gcode.3mf')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Inspect in Viewer/i }));
    expect(onOpenEnhanced).toHaveBeenCalledWith(99);
  });
});
