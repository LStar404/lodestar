const paths = {
  book: "M12 7C9 5 5 5 3 6v13c3-1 6-1 9 1 3-2 6-2 9-1V6c-2-1-6-1-9 1Zm0 0v13",
  notes: "M8 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V8M9 15l2-5 7-7 3 3-7 7-5 2Zm7-10 3 3M7 18h10",
  search: "M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z",
  spark: "m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3ZM20 2v4m-2-2h4",
  settings: "M4 7h16M4 17h16M9 4v6m6 4v6",
  down: "m7 10 5 5 5-5",
  upload: "M12 16V3m-5 5 5-5 5 5M4 15v5h16v-5",
  export: "M12 3v13m-5-5 5 5 5-5M4 17v4h16v-4",
  share: "m8 11 8-5m-8 7 8 5M8 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm14-8a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm0 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z",
  link: "m9 15 6-6m-5-3 2-2a5 5 0 0 1 7 7l-2 2m-3 5-2 2a5 5 0 0 1-7-7l2-2",
  expand: "M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5",
  left: "m14 6-6 6 6 6",
  right: "m10 6 6 6-6 6",
} as const;

export default function Icon({ name, size = 18 }: { name: keyof typeof paths; size?: number }) {
  return <svg className="ui-icon" width={size} height={size} viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d={paths[name]} />
  </svg>;
}
