# Breadcrumb Panel

A persistent, caret-aware breadcrumb panel for Sublime Text. Shows the lines
that caused the current indentation (class/def/if/…), click to jump, no popups
or phantoms. Dormant when hidden.

## Install (Package Control)
Search for **Breadcrumb Panel** once approved in the default channel.

## Usage
- Toggle: `Toggle Breadcrumb Panel` (add your own key binding).
- Optional: `Toggle Breadcrumb Panel Debug` for console traces.

## Settings (Preferences → Package Settings → Breadcrumb Panel)
```json
{
    "update_delay_ms": 32,
    "max_scan_lines": 5000,
    "debug": false
}
````

## License

Apache 2.0

Copyright (c) 2025, 7th software Limited.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
