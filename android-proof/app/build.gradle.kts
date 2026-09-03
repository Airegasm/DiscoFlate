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
        versionCode = 47
        versionName = "3.2.3"
        // Only the ABIs that (a) have Chaquopy wheels and (b) we actually run:
        // arm64-v8a = a real phone (Pixel 9); x86_64 = an emulator.
        ndk {
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }
    buildTypes {
        getByName("release") { isMinifyEnabled = false }
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
        buildPython("/usr/bin/python3.12")
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
