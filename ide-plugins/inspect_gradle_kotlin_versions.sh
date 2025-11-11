#!/usr/bin/env bash
set -eu

# Print Gradle wrapper distribution URL
if [ -f gradle/wrapper/gradle-wrapper.properties ]; then
  echo "== gradle-wrapper.properties =="
  grep -i distributionUrl gradle/wrapper/gradle-wrapper.properties || true
  echo
fi

# Print gradle.properties if present
if [ -f gradle.properties ]; then
  echo "== gradle.properties =="
  cat gradle.properties || true
  echo
fi

# Search for Kotlin Gradle plugin references
echo "== Kotlin plugin references (searching for kotlin-gradle-plugin and org.jetbrains.kotlin) =="
grep -R --line-number --color=never "kotlin-gradle-plugin" || true
grep -R --line-number --color=never "org.jetbrains.kotlin" || true

# Print build.gradle(.kts) files header lines to show plugin versions where declared
for f in $(git ls-files "*.gradle" "*.gradle.kts" 2>/dev/null || true); do
  echo "---- $f ----"
  sed -n '1,200p' "$f" | sed -n '1,60p'
  echo
done

# Print settings.gradle(.kts)
for f in $(git ls-files "settings.gradle" "settings.gradle.kts" 2>/dev/null || true); do
  echo "---- $f ----"
  sed -n '1,200p' "$f" | sed -n '1,60p'
  echo
done

# Print the kotlin plugin versions extracted via a rough regex
echo "== Extracted candidate versions =="
grep -R --line-number --color=never "kotlin-gradle-plugin[:=][^\n]*" || true
