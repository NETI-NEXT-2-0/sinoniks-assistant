export function registerFileDrop(target: Window, callbacks: {
  onActive: (value: boolean) => void;
  onFiles: (files: File[]) => void;
  isAvailable: () => boolean;
}): () => void;
