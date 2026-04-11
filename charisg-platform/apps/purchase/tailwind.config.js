import preset from '@charisg/ui/tailwind.preset';

export default {
  presets: [preset],
  content: [
    './index.html',
    './src/**/*.{js,jsx}',
    '../../packages/ui/src/**/*.{js,jsx}',
  ],
};
