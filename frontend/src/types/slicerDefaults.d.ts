import '../api/client';

declare module '../api/client' {
  interface AppSettings {
    slicer_default_printer_preset: string;
    slicer_default_process_preset: string;
    slicer_use_designer_settings: boolean;
  }
}
