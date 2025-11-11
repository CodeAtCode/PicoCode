#!/usr/bin/env bash
set -eu

# Print Gradle wrapper distribution URL
if [ -f ide-plugins/gradle/wrapper/gradle-wrapper.properties ]; then
  echo "== gradle-wrapper.properties =="
  grep -i distributionUrl ide-plugins/gradle/wrapper/gradle-wrapper.properties || true
  echo
fi

# Print gradle.properties if present
if [ -f ide-plugins/gradle.properties ]; then
  echo "== gradle.properties =="
  cat ide-plugins/gradle.properties || true
  echo
fi

# Search for Kotlin Gradle plugin references
echo "== Kotlin plugin references (searching for kotlin-gradle-plugin and org.jetbrains.kotlin) =="
grep -R --line-number --color=never "kotlin-gradle-plugin" ide-plugins/ || true
grep -R --line-number --color=never "org.jetbrains.kotlin" ide-plugins/ || true

# Print build.gradle(.kts) files header lines to show plugin versions where declared
for f in $(git ls-files "ide-plugins/*.gradle" "ide-plugins/*.gradle.kts" 2>/dev/null || true); do
  echo "---- $f ----"
  sed -n '1,200p' "$f" | sed -n '1,60p'
  echo
done

# Print settings.gradle(.kts)
for f in $(git ls-files "ide-plugins/settings.gradle" "ide-plugins/settings.gradle.kts" 2>/dev/null || true); do
  echo "---- $f ----"
  sed -n '1,200p' "$f" | sed -n '1,60p'
  echo
done

# Print the kotlin plugin versions extracted via a rough regex
echo "== Extracted candidate versions =="
grep -R --line-number --color=never "kotlin-gradle-plugin[:=][^\n]*" ide-plugins/ || true
