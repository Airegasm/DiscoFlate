import java.util.Properties

plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

android {
    namespace = "com.discoflate.proof"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.discoflate.proof"
        minSdk = 26
        targetSdk = 34
        versionCode = 62
        versionName = "3.8.0"
        // Only the ABIs that (a) have Chaquopy wheels and (b) we actually run:
        // arm64-v8a = a real phone (Pixel 9); x86_64 = an emulator.
        ndk {
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }
    signingConfigs {
        create("release") {
            // Created once; keystore.properties + the .keystore are git-ignored.
            // BACK BOTH UP — losing them breaks every user's in-place updates.
            val props = Properties()
            val f = rootProject.file("keystore.properties")
            if (f.exists()) {
                f.inputStream().use { props.load(it) }
                storeFile = rootProject.file(props.getProperty("storeFile"))
                storePassword = props.getProperty("storePassword")
                keyAlias = props.getProperty("keyAlias")
                keyPassword = props.getProperty("keyPassword")
            }
        }
    }
    buildTypes {
        getByName("release") {
            isMinifyEnabled = false
            isDebuggable = false
            signingConfig = signingConfigs.getByName("release")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

chaquopy {
    defaultConfig {
        // Python 3.12 is the newest Chaquopy 15.x target that has Android wheels
        // for the whole aiohttp stack WITHOUT needing propcache (absent upstream).
        version = "3.12"
        // Machine-specific path comes from local.properties (buildPython=…);
        // falls back to python3.12 on PATH.
        val lp = Properties()
        val lf = rootProject.file("local.properties")
        if (lf.exists()) lf.inputStream().use { lp.load(it) }
        buildPython(lp.getProperty("buildPython") ?: "python3.12")
        pip {
            // discord.py is pure-Python (from PyPI); the four C-extension deps are
            // pinned to the exact versions Chaquopy prebuilds for android/cp312.
            install("discord.py==2.4.0")
            install("aiohttp==3.9.1")
            install("yarl==1.9.3")
            install("multidict==6.0.4")
            install("frozenlist==1.4.0")
            install("pyaes")   // pure-Python AES for the Tapo KLAP driver
        }
    }
}
