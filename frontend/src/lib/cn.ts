import { clsx, type ClassValue } from "clsx";

// Single place that merges conditional class names so components stay readable.
export function cn(...inputs: ClassValue[]): string {
  return clsx(inputs);
}