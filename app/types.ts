export type LibraryFormat = {
  consolidated_format_name: string;
  original_labels: string[];
  fixed_structure: string;
  variable_elements: string;
  example_count: number;
  video_ids: string[];
};

export type LibraryVideo = {
  username: string;
  caption: string;
  hook: string;
  core_action: string;
  payoff_or_cta: string;
  likesCount: string;
  videoViewCount: string;
  consolidated_format: string;
  format_category: string;
  permalink: string;
  thumbnail: string;
  video_filename: string;
};

export type LibraryData = {
  model: string;
  formats: LibraryFormat[];
  videos: LibraryVideo[];
};
