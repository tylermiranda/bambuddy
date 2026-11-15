import { createContext, lazy, Suspense, useCallback, useContext, useEffect, useMemo, useState, type ComponentType, type ReactNode } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useLocation } from 'react-router-dom';
import { api } from '../api/client';
import { useAuth } from './AuthContext';
import { listenForCameraPopIn, registerCameraTab } from '../utils/camera';
import { recoverFromMissingChunk } from '../utils/lazyPage';
import type { EmbeddedCameraViewerProps } from '../components/EmbeddedCameraViewer';

/**
 * The floating camera overlays (#2737).
 *
 * One opened from the sidebar's camera button is shown on every page. One
 * opened from a printer card or the camera wall belongs to the Printers page:
 * it is hidden on the other pages, still open, and back on returning there.
 * Persisted under the same key as before #2737, so overlays left open before
 * an update are still open after it; an entry without a scope is one from
 * before the scope existed, and stays on every page as it was.
 */
const STORAGE_KEY = 'openEmbeddedCameras';

// Loaded on first use: every page now carries the host, and most sessions
// never open an overlay (#3175 keeps the first load small).
// A file missing after an update reloads the page, as for a page (lazyPage).
// One that still fails leaves the overlay out: the host sits above the app's
// layout, so throwing here would replace every page with the error screen.
const EmbeddedCameraViewer = lazy(async (): Promise<{ default: ComponentType<EmbeddedCameraViewerProps> }> => {
  try {
    const m = await import('../components/EmbeddedCameraViewer');
    return { default: m.EmbeddedCameraViewer };
  } catch (error) {
    if (await recoverFromMissingChunk()) return new Promise<never>(() => {});
    console.error('Could not load the camera overlay:', error);
    return { default: () => null };
  }
});

/** Where an overlay is shown: every page, or the Printers page only. */
export type CameraScope = 'app' | 'printers';

export interface OpenCamera {
  id: number;
  name: string;
  scope: CameraScope;
  source?: 'chamber' | 'external';
}

function cameraKey(id: number, source?: 'chamber' | 'external'): string {
  return source ? `${id}:${source}` : `${id}`;
}

interface EmbeddedCameraContextType {
  openCameras: OpenCamera[];
  /** Opening one already open for every page keeps it there; opening a
   *  Printers-page one for every page moves it to every page. */
  openEmbeddedCamera: (id: number, name: string, scope: CameraScope, source?: 'chamber' | 'external') => void;
  closeEmbeddedCamera: (id: number, source?: 'chamber' | 'external') => void;
}

const EmbeddedCameraContext = createContext<EmbeddedCameraContextType | undefined>(undefined);

function readStored(): OpenCamera[] {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) {
      const cameras = JSON.parse(saved) as OpenCamera[];
      if (Array.isArray(cameras)) {
        return cameras
          .filter(c => c && Number.isInteger(c.id) && c.id > 0)
          .map(c => ({
            id: c.id,
            name: String(c.name ?? ''),
            scope: c.scope === 'printers' ? 'printers' : 'app',
            source: c.source === 'external' ? 'external' : c.source === 'chamber' ? 'chamber' : undefined,
          }));
      }
    }
  } catch {
    // Unreadable storage or a corrupt entry: start with none open.
  }
  return [];
}

/**
 * Applies one tab's change to the stored list, read fresh each time: every
 * open tab writes it, and writing this tab's whole list would drop what the
 * others opened.
 */
function updateStored(change: (cameras: OpenCamera[]) => OpenCamera[]) {
  try {
    const next = change(readStored());
    if (next.length > 0) {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    } else {
      localStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    // Overlays stay open in this tab; only reopening after a reload is lost.
  }
}

export function EmbeddedCameraProvider({ children }: { children: ReactNode }) {
  const { hasPermission } = useAuth();
  // Which printers may be shown is only known from the printer list, which
  // takes printers:read; without it the Printers page, where a camera opened
  // from before, is out of reach too.
  const canView = hasPermission('camera:view') && hasPermission('printers:read');
  const [cameras, setCameras] = useState<Map<string, OpenCamera>>(
    () => new Map(readStored().map(c => [cameraKey(c.id, c.source), c])),
  );

  const openEmbeddedCamera = useCallback((id: number, name: string, scope: CameraScope, source?: 'chamber' | 'external') => {
    // The wider of the two scopes wins, so a card can't pull a camera the
    // sidebar put on every page back onto one.
    const key = cameraKey(id, source);
    const merge = (current: OpenCamera | undefined): OpenCamera =>
      current
        ? { ...current, scope: current.scope === 'app' || scope === 'app' ? 'app' : 'printers', source: source || current.source }
        : { id, name, scope, source };
    setCameras(prev => {
      const next = merge(prev.get(key));
      const current = prev.get(key);
      return current && current.scope === next.scope && current.source === next.source ? prev : new Map(prev).set(key, next);
    });
    updateStored(list =>
      list.some(c => c.id === id && c.source === source)
        ? list.map(c => (c.id === id && c.source === source ? merge(c) : c))
        : [...list, { id, name, scope, source }],
    );
  }, []);

  const closeEmbeddedCamera = useCallback((id: number, source?: 'chamber' | 'external') => {
    setCameras(prev => {
      const next = new Map(prev);
      if (source !== undefined) {
        next.delete(cameraKey(id, source));
        next.delete(cameraKey(id));
      } else {
        for (const [k, v] of prev.entries()) {
          if (v.id === id) next.delete(k);
        }
      }
      return next;
    });
    updateStored(list => {
      if (source !== undefined) {
        return list.filter(c => !(c.id === id && (c.source === source || (!c.source && source === 'chamber'))));
      }
      return list.filter(c => c.id !== id);
    });
  }, []);

  // The list is stored per browser, not per user: a printer that was deleted,
  // or one the user now signed in may not see, is dropped once the printers
  // they can see are known, rather than showing up as a broken overlay. Only
  // a list fetched since this mounted counts: the cache can still hold the
  // last user's, and a fresh one would not be fetched again by itself.
  const queryClient = useQueryClient();
  const [mountedAt] = useState(() => Date.now());
  const { data: printers, dataUpdatedAt, isSuccess, isError } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
    enabled: canView && cameras.size > 0,
    // Stale at once, so it is fetched on mount and whenever an overlay opens.
    staleTime: 0,
    refetchOnWindowFocus: false,
  });
  const listFresh = isSuccess && dataUpdatedAt >= mountedAt;
  const listChecked = listFresh || isError;
  useEffect(() => {
    if (!listFresh || !printers) return;
    const visible = new Set(printers.map(p => p.id));
    // Also for an overlay added after the list came in.
    const dropped = Array.from(cameras.values()).filter(c => !visible.has(c.id));
    dropped.forEach(c => closeEmbeddedCamera(c.id, c.source));
  }, [listFresh, printers, cameras, closeEmbeddedCamera]);

  useEffect(() => {
    if (!canView) return;
    // A camera window is told done only for a printer this tab's user can
    // see, checked against the list as it is now.
    const canShow = async (printerId: number) => {
      const list = await queryClient.fetchQuery({ queryKey: ['printers'], queryFn: api.getPrinters, staleTime: 0 });
      return list.some(p => p.id === printerId);
    };
    // Brought back into this tab, so onto whichever page it is on.
    return listenForCameraPopIn(registerCameraTab(), (id, name) => openEmbeddedCamera(id, name, 'app'), canShow);
  }, [canView, openEmbeddedCamera, queryClient]);

  const openCameras = useMemo(() => Array.from(cameras.values()), [cameras]);
  const onPrintersPage = useLocation().pathname === '/';
  const shownCameras = useMemo(
    () => openCameras.filter(c => c.scope === 'app' || onPrintersPage),
    [openCameras, onPrintersPage],
  );
  const value = useMemo(
    () => ({ openCameras, openEmbeddedCamera, closeEmbeddedCamera }),
    [openCameras, openEmbeddedCamera, closeEmbeddedCamera],
  );

  return (
    <EmbeddedCameraContext.Provider value={value}>
      {children}
      {/* Several can be open at once; each offsets itself by its index. */}
      {canView && listChecked && shownCameras.length > 0 && (
        <Suspense fallback={null}>
          {shownCameras.map((camera, index) => (
            <EmbeddedCameraViewer
              key={`${camera.id}-${camera.source || 'default'}`}
              printerId={camera.id}
              printerName={camera.name}
              source={camera.source}
              viewerIndex={index}
              onClose={() => closeEmbeddedCamera(camera.id, camera.source)}
              onSpawnSecondCamera={(targetSource) => openEmbeddedCamera(camera.id, camera.name, 'app', targetSource)}
            />
          ))}
        </Suspense>
      )}
    </EmbeddedCameraContext.Provider>
  );
}

export function useEmbeddedCameras(): EmbeddedCameraContextType {
  const context = useContext(EmbeddedCameraContext);
  if (!context) {
    throw new Error('useEmbeddedCameras must be used inside EmbeddedCameraProvider');
  }
  return context;
}
